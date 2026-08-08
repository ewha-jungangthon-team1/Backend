import json
import sys
import time
import urllib.request

BASE_URL = "http://localhost:8000"


def post(url):
    req = urllib.request.Request(url, method="POST", data=b"")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url):
    with urllib.request.urlopen(url) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    if len(sys.argv) < 2:
        print("usage: python check_polling.py <public_token>")
        print("example: python check_polling.py 3fa85f64-5717-4562-b3fc-2c963f66afa6")
        return

    public_token = sys.argv[1]

    ensure_url = "{}/api/bags/{}/live-sessions/ensure/".format(BASE_URL, public_token)
    print("calling ensure...")
    result = post(ensure_url)
    print("ensure response:", result)
    print("")

    session_id = result["session_id"]
    interval = result.get("polling_interval_seconds", 2)
    reading_url = "{}/api/sessions/{}/latest-reading/".format(BASE_URL, session_id)

    start = time.time()
    while True:
        elapsed = round(time.time() - start)
        reading = get(reading_url)
        print(
            "[{:>4}s] seq={} strap_load={} strap_strain={} humidity={} moisture_detected={} temperature={} progress={} finished={}".format(
                elapsed,
                reading["sequence"],
                reading["strap_load"],
                reading["strap_strain"],
                reading["humidity"],
                reading["moisture_detected"],
                reading["temperature"],
                reading["progress_ratio"],
                reading["is_finished"],
            )
        )
        if reading["is_finished"]:
            print("")
            print("done: session finished")
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
