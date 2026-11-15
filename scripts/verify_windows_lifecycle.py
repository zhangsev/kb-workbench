"""Exercise actual Windows status/stop commands while keeping serve stdin open."""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def command(*args):
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
           str(ROOT / "start.ps1"), *args]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    print("$", " ".join(cmd), flush=True)
    print(result.stdout, result.stderr, f"exit={result.returncode}", flush=True)
    assert result.returncode == 0
    return result.stdout


def main():
    before = json.loads(command("status", "--json"))
    assert not before["running_gateways"], before["running_gateways"]
    with (ROOT / "docs/windows-logs/lifecycle-serve-stderr.txt").open("w", encoding="utf-8") as err:
        child = subprocess.Popen([sys.executable, "-m", "kbw", "serve"], cwd=ROOT,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
        try:
            for _ in range(100):
                if list((ROOT / ".kbw/run").glob("*.json")):
                    break
                if child.poll() is not None:
                    raise AssertionError(f"serve exited early: {child.returncode}")
                time.sleep(.1)
            running = json.loads(command("status", "--json"))
            assert running["running_gateways"], "status did not find the live gateway"
            pids = [g["pid"] for g in running["running_gateways"]]
            print("Verified live gateway PIDs:", pids, flush=True)
            command("stop")
            child.wait(timeout=15)
            after = json.loads(command("status", "--json"))
            assert not after["running_gateways"], after["running_gateways"]
            print("PASS: stdin remained open; status listed gateway; stop terminated it.", flush=True)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=15)
            child.stdin.close()
            child.stdout.close()


if __name__ == "__main__":
    main()
