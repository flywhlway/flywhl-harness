import sys

from messages import greeting

if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "world"
    print(greeting(name))
