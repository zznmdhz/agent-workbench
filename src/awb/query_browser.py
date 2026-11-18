"""Invalidated, bounded range projection for the session browser.

All matching sessions contribute to counts/filters. SQL applies ordering and LIMIT
before any row payload is decoded or local filesystem metadata is inspected.
Content/file searches retain the complete conservative reference path, not a
truncated first-page search. Only eight lightweight range projections are kept.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import archive
from .multi_usage import _range
from .query_model import model

_SORT_FIELDS = {'recent':'last_at','oldest':'last_at','text_desc':'text_chars',
    'text_asc':'text_chars','duration_desc':'agent_ms','duration_asc':'agent_ms',
    'storage_desc':'storage_bytes','storage_asc':'storage_bytes',
    'messages_desc':'messages','messages_asc':'messages','files_desc':'file_count'}
_ASCENDING = {'oldest','text_asc','duration_asc','storage_asc','messages_asc'}
_MAX_RANGES = 8


def _schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS browser_ranges(
        range_key TEXT PRIMARY KEY,generation TEXT NOT NULL,last_use INTEGER NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS browser_rows(
        range_key TEXT NOT NULL,ordinal INTEGER NOT NULL,agent TEXT NOT NULL,
        source_id TEXT,profile TEXT,title_fold TEXT NOT NULL,last_at TEXT NOT NULL,
        text_chars INTEGER NOT NULL,agent_ms INTEGER NOT NULL,storage_bytes INTEGER,
        messages INTEGER NOT NULL,file_count INTEGER NOT NULL,payload_json TEXT NOT NULL,
        PRIMARY KEY(range_key,ordinal))''')
    for field in ('last_at','text_chars','agent_ms','storage_bytes','messages','file_count'):
        c.execute(f'CREATE INDEX IF NOT EXISTS ix_browser_{field} ON browser_rows(range_key,{field},last_at,ordinal)')


def browser(db, day, through, tz, *, agent=None, query='', device_id=None,
            search_in='all', sort='recent', min_text=0, min_duration=0,
            has_files=False, limit=300, source_id=None, profile=None, offset=0, session_scope='conversation'):
    from .archive_views import AGENTS, _browser_uncached
    from .session_identity import scope_sql
    scope_condition = scope_sql(session_scope)
    if sort not in _SORT_FIELDS or search_in not in {'all','title','content','file'}:
        raise ValueError('Invalid session filter')
    if min_text < 0 or min_duration < 0 or not 1 <= limit <= 1000 or offset < 0:
        raise ValueError('Invalid session filter')
    _range(day, through, tz)  # Keep validation even on a warm range.
    needle = query.strip().casefold()
    if len(needle) > 120:
        raise ValueError('Search term too long')
    if (sort == 'recent' and not needle and min_text == 0 and min_duration == 0
            and not has_files and not source_id and profile is None):
        from .session_catalog import catalog
        return catalog(db).query(day, through, tz, device_id, agent=agent,
                                 query=query, limit=limit, offset=offset, session_scope=session_scope)
    if needle and search_in != 'title':
        return _browser_uncached(db, day, through, tz, agent=agent, query=query,
            device_id=device_id, search_in=search_in, sort=sort, min_text=min_text,
            min_duration=min_duration, has_files=has_files, limit=limit,
            source_id=source_id, profile=profile, _offset=offset, session_scope=session_scope)
    range_key = json.dumps(('identity-v1',day,through,tz,agent,device_id), ensure_ascii=False)
    with model(db) as projection:
        c = projection.conn
        _schema(c)
        generation = c.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        # Any changed archive fact/alias invalidates derived activity, file counts,
        # metadata and sort keys. Unrelated service writes do not stale the range.
        c.execute('DELETE FROM browser_rows WHERE range_key IN (SELECT range_key FROM browser_ranges WHERE generation<>?)',(generation,))
        c.execute('DELETE FROM browser_ranges WHERE generation<>?',(generation,))
        found = c.execute('SELECT 1 FROM browser_ranges WHERE range_key=?',(range_key,)).fetchone()
        tick = c.execute('SELECT COALESCE(max(last_use),0)+1 FROM browser_ranges').fetchone()[0]
        if found is None:
            # No limit/filter is applied during construction. Aggregate-only and
            # run-only sessions must remain represented with honest zero values.
            rows = _browser_uncached(db,day,through,tz,agent=agent,device_id=device_id,_base=True)
            c.executemany('INSERT INTO browser_rows VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                ((range_key,i,r['agent'],r.get('source_id'),r.get('profile'),r['title'].casefold(),
                  r['last_at'],r['text_chars'],r['agent_ms'],r.get('storage_bytes'),
                  r['messages'],r['file_count'],json.dumps(r,ensure_ascii=False)) for i,r in enumerate(rows)))
            c.execute('INSERT INTO browser_ranges VALUES(?,?,?)',(range_key,generation,tick))
        else:
            c.execute('UPDATE browser_ranges SET last_use=? WHERE range_key=?',(tick,range_key))
        stale = [r[0] for r in c.execute('SELECT range_key FROM browser_ranges ORDER BY last_use DESC LIMIT -1 OFFSET ?',(_MAX_RANGES,))]
        for key in stale:
            c.execute('DELETE FROM browser_rows WHERE range_key=?',(key,))
            c.execute('DELETE FROM browser_ranges WHERE range_key=?',(key,))
        where, params = ['range_key=?','text_chars>=?','agent_ms>=?',scope_condition], [range_key,min_text,min_duration*60000]
        if source_id:
            where.append('source_id=?')
            params.append(source_id)
        if profile is not None:
            where.append('profile=?')
            params.append(profile)
        if has_files:
            where.append('file_count>0')
        if needle:
            where.append('instr(title_fold,?)>0')
            params.append(needle)
        condition = ' AND '.join(where)
        counts = dict.fromkeys(AGENTS,0)
        count = 0
        for row in c.execute('SELECT agent,count(*) FROM browser_rows WHERE '+condition+' GROUP BY agent',params):
            count += row[1]
            if row[0] in counts:
                counts[row[0]] = row[1]
        field = _SORT_FIELDS[sort]
        # Python's stable sort used -1 for unknown descending storage and +inf
        # ascending. Keep those exact null rules and the original tie ordinal.
        ordering = (f'{field} IS NULL,{field} ASC,last_at ASC,ordinal ASC' if sort in _ASCENDING else
                    f'COALESCE({field},-1) DESC,last_at DESC,ordinal ASC')
        items = [json.loads(r[0]) for r in c.execute('SELECT payload_json FROM browser_rows WHERE '+condition+
                 ' ORDER BY '+ordering+' LIMIT ? OFFSET ?',(*params,limit,offset))]
    # Filesystem existence can change without an archive write: never cache it.
    local_id = archive.local_device_id(db)
    for row in items:
        sources = row.pop('_sources', [])
        row['can_open_folder'] = row['device_id'] == local_id and any(
            Path(raw).parent.is_dir() for raw in sources)
        if needle:
            row['match'] = {'type':'标题','excerpt':row['title']}
    return {'day':day,'through':through,'query':query,'counts':counts,
            'session_count':count,'sessions':items,'sort':sort}
