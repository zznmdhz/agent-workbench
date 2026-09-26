import sys

from awb.desktop import main

if __name__ == "__main__":
    sys.argv.append("--reset-password")
    main()
