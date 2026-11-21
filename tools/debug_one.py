import json, os, sys
sys.path.insert(0, "/app")
from runner import overlay, verify
def main():
    os.environ["BR_DEBUG"] = "1"
    if os.environ.get("WARM"): verify.warm_up()
    tree = overlay.build("/opt/sqlglot", "/tmp/w/x", {})
    print(verify.run(tree, sys.argv[1:], 300), file=sys.stderr)
if __name__ == "__main__":
    main()
