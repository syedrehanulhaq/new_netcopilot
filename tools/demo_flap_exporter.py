from http.server import BaseHTTPRequestHandler, HTTPServer
import time


HOST = "127.0.0.1"
PORT = 9105


def _metrics_payload() -> str:
    now = int(time.time())
    samples = [
        {
            "device": "iosxr-pe-1",
            "interface": "TenGigE0/0/0/0",
            "flaps": 15,
            "last_flap": now - 600,
            "description": "WAN uplink",
            "region": "EU",
        },
        {
            "device": "iosxr-pe-2",
            "interface": "TenGigE0/0/0/1",
            "flaps": 9,
            "last_flap": now - 1800,
            "description": "DCI link",
            "region": "US",
        },
        {
            "device": "iosxr-pe-3",
            "interface": "TenGigE0/0/0/2",
            "flaps": 4,
            "last_flap": now - 4200,
            "description": "Access ring",
            "region": "APAC",
        },
    ]
    lines = [
        "# HELP netcopilot_link_flaps_total Interface flap counter (demo)",
        "# TYPE netcopilot_link_flaps_total counter",
    ]
    for sample in samples:
        labels = (
            f'device="{sample["device"]}",'
            f'interface="{sample["interface"]}",'
            f'description="{sample["description"]}",'
            f'region="{sample["region"]}"'
        )
        lines.append(f"netcopilot_link_flaps_total{{{labels}}} {sample['flaps']}")
    lines += [
        "# HELP netcopilot_link_last_flap_timestamp_seconds Last flap time (epoch)",
        "# TYPE netcopilot_link_last_flap_timestamp_seconds gauge",
    ]
    for sample in samples:
        labels = (
            f'device="{sample["device"]}",'
            f'interface="{sample["interface"]}",'
            f'description="{sample["description"]}",'
            f'region="{sample["region"]}"'
        )
        lines.append(
            f"netcopilot_link_last_flap_timestamp_seconds{{{labels}}} {sample['last_flap']}"
        )
    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        payload = _metrics_payload().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    server = HTTPServer((HOST, PORT), MetricsHandler)
    print(f"Demo flap exporter listening on http://{HOST}:{PORT}/metrics")
    server.serve_forever()


if __name__ == "__main__":
    main()
