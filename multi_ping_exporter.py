#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Multi-target ping exporter for Prometheus.

The exporter probes targets in the background and serves cached metrics from
/metrics. This avoids the scrape-time burst that can make blackbox_exporter
ICMP probes look flaky when many targets are scraped at once.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import math
import platform
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


PING_TIME_RE = re.compile(r"time[=<]\s*(\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)
LINUX_PACKET_RE = re.compile(r"(\d+)\s+packets transmitted,\s+(\d+)\s+(?:packets\s+)?received", re.IGNORECASE)
WINDOWS_PACKET_RE = re.compile(r"Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+)", re.IGNORECASE)
LINUX_RTT_RE = re.compile(r"(?:rtt|round-trip).*?=\s*([\d.]+)/([\d.]+)/([\d.]+)/([\d.]+)\s*ms", re.IGNORECASE)


@dataclasses.dataclass(slots=True)
class TargetConfig:
    target: str
    labels: dict[str, str]
    enabled: bool = True
    comment: str = ""


@dataclasses.dataclass(slots=True)
class ExporterConfig:
    listen: str
    probe_interval_seconds: float
    probe_timeout_seconds: float
    ping_count: int
    success_required: int
    workers: int
    jitter_seconds: float
    window_size: int
    targets: list[TargetConfig]


@dataclasses.dataclass(slots=True)
class ProbeResult:
    target: str
    ok: bool
    sent: int
    received: int
    packet_loss_ratio: float
    rtt_ms: float | None
    min_rtt_ms: float | None
    max_rtt_ms: float | None
    duration_seconds: float
    return_code: int
    timestamp: float
    error: str


@dataclasses.dataclass
class TargetState:
    config: TargetConfig
    last_result: ProbeResult | None = None
    attempts_total: int = 0
    failures_total: int = 0
    consecutive_failures: int = 0
    last_success_timestamp: float = 0.0
    recent_successes: list[int] = dataclasses.field(default_factory=list)


def parse_listen(value: str) -> tuple[str, int]:
    if ":" not in value:
        return value, 9116
    host, port_text = value.rsplit(":", 1)
    return host or "0.0.0.0", int(port_text)


def parse_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "off", "disabled"}:
            return False
    raise ValueError(f"invalid boolean value: {value!r}")


def load_config(path: str) -> ExporterConfig:
    with open(path, "r", encoding="utf-8") as file_obj:
        raw = json.load(file_obj)

    targets: list[TargetConfig] = []
    for item in raw.get("targets", []):
        target = str(item["target"]).strip()
        enabled = parse_bool(item.get("enabled", True))
        comment = str(item.get("comment", "")).strip()
        labels = {
            str(k): str(v)
            for k, v in item.items()
            if k not in {"target", "enabled", "comment"} and v is not None
        }
        targets.append(TargetConfig(target=target, labels=labels, enabled=enabled, comment=comment))

    if not targets:
        raise ValueError("config must contain at least one target")

    ping_count = int(raw.get("ping_count", 5))
    success_required = int(raw.get("success_required", 1))
    if ping_count < 1:
        raise ValueError("ping_count must be >= 1")
    if success_required < 1 or success_required > ping_count:
        raise ValueError("success_required must be between 1 and ping_count")

    return ExporterConfig(
        listen=str(raw.get("listen", "0.0.0.0:9116")),
        probe_interval_seconds=float(raw.get("probe_interval_seconds", 15.0)),
        probe_timeout_seconds=float(raw.get("probe_timeout_seconds", 3.0)),
        ping_count=ping_count,
        success_required=success_required,
        workers=int(raw.get("workers", 20)),
        jitter_seconds=float(raw.get("jitter_seconds", 0.05)),
        window_size=int(raw.get("window_size", 20)),
        targets=targets,
    )


def build_ping_command(target: str, count: int, timeout_seconds: float) -> list[str]:
    system = platform.system().lower()
    if system == "windows":
        return ["ping", "-n", str(count), "-w", str(max(1, int(timeout_seconds * 1000))), target]

    timeout_arg = str(max(1, int(math.ceil(timeout_seconds))))
    return ["ping", "-n", "-c", str(count), "-W", timeout_arg, target]


def parse_ping_output(output: str, default_sent: int) -> tuple[int, int, float | None, float | None, float | None]:
    sent = default_sent
    received = 0

    linux_match = LINUX_PACKET_RE.search(output)
    windows_match = WINDOWS_PACKET_RE.search(output)
    if linux_match:
        sent = int(linux_match.group(1))
        received = int(linux_match.group(2))
    elif windows_match:
        sent = int(windows_match.group(1))
        received = int(windows_match.group(2))
    else:
        received = len(PING_TIME_RE.findall(output))

    rtt_values = [float(value) for value in PING_TIME_RE.findall(output)]
    rtt_match = LINUX_RTT_RE.search(output)
    if rtt_match:
        min_rtt = float(rtt_match.group(1))
        avg_rtt = float(rtt_match.group(2))
        max_rtt = float(rtt_match.group(3))
    elif rtt_values:
        min_rtt = min(rtt_values)
        avg_rtt = sum(rtt_values) / len(rtt_values)
        max_rtt = max(rtt_values)
    else:
        min_rtt = None
        avg_rtt = None
        max_rtt = None

    return sent, received, avg_rtt, min_rtt, max_rtt


def ping_target(target: str, count: int, timeout_seconds: float, success_required: int) -> ProbeResult:
    started = time.monotonic()
    timestamp = time.time()
    command = build_ping_command(target, count, timeout_seconds)
    timeout = max(count * timeout_seconds + 3.0, 5.0)

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=timeout,
        )
        raw_output = (completed.stdout or "") + (completed.stderr or "")
        sent, received, avg_rtt, min_rtt, max_rtt = parse_ping_output(raw_output, count)
        error = ""
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        raw_output = ((exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or ""))
        raw_error = ((exc.stderr or b"") if isinstance(exc.stderr, bytes) else (exc.stderr or ""))
        if isinstance(raw_output, bytes):
            raw_output = raw_output.decode("utf-8", errors="ignore")
        if isinstance(raw_error, bytes):
            raw_error = raw_error.decode("utf-8", errors="ignore")
        sent, received, avg_rtt, min_rtt, max_rtt = parse_ping_output(str(raw_output) + str(raw_error), count)
        error = f"ping command timeout after {timeout:.1f}s"
        return_code = 124
    except OSError as exc:
        sent, received, avg_rtt, min_rtt, max_rtt = count, 0, None, None, None
        error = str(exc)
        return_code = 127

    duration = time.monotonic() - started
    packet_loss_ratio = 1.0 if sent <= 0 else max(0.0, min(1.0, (sent - received) / sent))
    ok = received >= success_required
    return ProbeResult(
        target=target,
        ok=ok,
        sent=sent,
        received=received,
        packet_loss_ratio=packet_loss_ratio,
        rtt_ms=avg_rtt,
        min_rtt_ms=min_rtt,
        max_rtt_ms=max_rtt,
        duration_seconds=duration,
        return_code=return_code,
        timestamp=timestamp,
        error=error,
    )


def probe_reason(result: ProbeResult | None, enabled: bool, success_required: int) -> str:
    """Return a bounded reason label suitable for Prometheus metrics."""
    if not enabled:
        return "disabled"
    if result is None:
        return "not_probed"
    if result.ok:
        return "ok"
    if result.return_code == 124:
        return "command_timeout"
    if result.return_code == 127:
        return "command_error"
    if result.received == 0:
        return "no_reply"
    if result.received < success_required:
        return "insufficient_replies"
    return "probe_failed"


def label_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metric_labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    parts = [f'{key}="{label_escape(value)}"' for key, value in sorted(labels.items())]
    return "{" + ",".join(parts) + "}"


def metric_line(name: str, labels: dict[str, str], value: float | int | str) -> str:
    return f"{name}{metric_labels(labels)} {value}"


class PingExporter:
    def __init__(self, config: ExporterConfig) -> None:
        self.config = config
        self.states = {target.target: TargetState(config=target) for target in config.targets}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.work_queue: queue.Queue[TargetConfig] = queue.Queue()

    def start(self) -> None:
        for index in range(max(1, self.config.workers)):
            thread = threading.Thread(target=self.worker_loop, name=f"ping-worker-{index + 1}", daemon=True)
            thread.start()
        scheduler = threading.Thread(target=self.scheduler_loop, name="ping-scheduler", daemon=True)
        scheduler.start()

    def stop(self) -> None:
        self.stop_event.set()

    def scheduler_loop(self) -> None:
        while not self.stop_event.is_set():
            round_started = time.monotonic()
            for target in self.config.targets:
                if not target.enabled:
                    continue
                self.work_queue.put(target)
                if self.config.jitter_seconds > 0:
                    self.stop_event.wait(self.config.jitter_seconds)

            self.work_queue.join()
            elapsed = time.monotonic() - round_started
            wait_seconds = max(0.0, self.config.probe_interval_seconds - elapsed)
            self.stop_event.wait(wait_seconds)

    def worker_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                target = self.work_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            result = ping_target(
                target.target,
                self.config.ping_count,
                self.config.probe_timeout_seconds,
                self.config.success_required,
            )
            self.record_result(target.target, result)
            self.work_queue.task_done()

    def record_result(self, target: str, result: ProbeResult) -> None:
        with self.lock:
            state = self.states[target]
            state.last_result = result
            state.attempts_total += 1
            if result.ok:
                state.consecutive_failures = 0
                state.last_success_timestamp = result.timestamp
            else:
                state.failures_total += 1
                state.consecutive_failures += 1

            state.recent_successes.append(1 if result.ok else 0)
            if len(state.recent_successes) > self.config.window_size:
                state.recent_successes = state.recent_successes[-self.config.window_size :]

    def render_metrics(self) -> str:
        lines = [
            "# HELP ping_exporter_targets Number of configured ping targets.",
            "# TYPE ping_exporter_targets gauge",
            f"ping_exporter_targets {len(self.states)}",
            "# HELP ping_target_info Static target metadata. Value is always 1.",
            "# TYPE ping_target_info gauge",
            "# HELP ping_target_enabled Whether a target is enabled for active probing.",
            "# TYPE ping_target_enabled gauge",
            "# HELP ping_exporter_queue_size Number of waiting probe jobs.",
            "# TYPE ping_exporter_queue_size gauge",
            f"ping_exporter_queue_size {self.work_queue.qsize()}",
            "# HELP ping_probe_success Whether the last probe succeeded. 1 means success, 0 means failure.",
            "# TYPE ping_probe_success gauge",
            "# HELP ping_probe_received_packets Number of packets received by the last probe.",
            "# TYPE ping_probe_received_packets gauge",
            "# HELP ping_probe_sent_packets Number of packets sent by the last probe.",
            "# TYPE ping_probe_sent_packets gauge",
            "# HELP ping_probe_packet_loss_ratio Packet loss ratio of the last probe.",
            "# TYPE ping_probe_packet_loss_ratio gauge",
            "# HELP ping_probe_rtt_seconds Average round-trip time of the last probe.",
            "# TYPE ping_probe_rtt_seconds gauge",
            "# HELP ping_probe_min_rtt_seconds Minimum round-trip time of the last probe.",
            "# TYPE ping_probe_min_rtt_seconds gauge",
            "# HELP ping_probe_max_rtt_seconds Maximum round-trip time of the last probe.",
            "# TYPE ping_probe_max_rtt_seconds gauge",
            "# HELP ping_probe_duration_seconds Wall-clock duration of the last probe.",
            "# TYPE ping_probe_duration_seconds gauge",
            "# HELP ping_probe_consecutive_failures Consecutive failed probes for this target.",
            "# TYPE ping_probe_consecutive_failures gauge",
            "# HELP ping_probe_attempts_total Total probe attempts for this target.",
            "# TYPE ping_probe_attempts_total counter",
            "# HELP ping_probe_failures_total Total failed probe attempts for this target.",
            "# TYPE ping_probe_failures_total counter",
            "# HELP ping_probe_last_probe_timestamp_seconds Unix timestamp of the last probe.",
            "# TYPE ping_probe_last_probe_timestamp_seconds gauge",
            "# HELP ping_probe_last_success_timestamp_seconds Unix timestamp of the last successful probe.",
            "# TYPE ping_probe_last_success_timestamp_seconds gauge",
            "# HELP ping_probe_window_success_ratio Success ratio of the recent probe window.",
            "# TYPE ping_probe_window_success_ratio gauge",
            "# HELP ping_probe_reason Last probe result reason. Value is always 1.",
            "# TYPE ping_probe_reason gauge",
        ]

        with self.lock:
            states = list(self.states.values())

        for state in states:
            labels = {"target": state.config.target, **state.config.labels}
            info_labels = {"target": state.config.target, **state.config.labels}
            if state.config.comment:
                info_labels["comment"] = state.config.comment
            result = state.last_result

            lines.append(metric_line("ping_target_info", info_labels, 1))
            lines.append(metric_line("ping_target_enabled", labels, 1 if state.config.enabled else 0))
            lines.append(metric_line("ping_probe_attempts_total", labels, state.attempts_total))
            lines.append(metric_line("ping_probe_failures_total", labels, state.failures_total))
            lines.append(metric_line("ping_probe_consecutive_failures", labels, state.consecutive_failures))
            lines.append(metric_line("ping_probe_last_success_timestamp_seconds", labels, int(state.last_success_timestamp)))
            reason_labels = {**labels, "reason": probe_reason(result, state.config.enabled, self.config.success_required)}
            lines.append(metric_line("ping_probe_reason", reason_labels, 1))

            if not state.config.enabled:
                lines.append(metric_line("ping_probe_window_success_ratio", labels, 0))
                lines.append(metric_line("ping_probe_success", labels, 0))
                lines.append(metric_line("ping_probe_sent_packets", labels, 0))
                lines.append(metric_line("ping_probe_received_packets", labels, 0))
                lines.append(metric_line("ping_probe_packet_loss_ratio", labels, 1))
                lines.append(metric_line("ping_probe_rtt_seconds", labels, 0))
                lines.append(metric_line("ping_probe_min_rtt_seconds", labels, 0))
                lines.append(metric_line("ping_probe_max_rtt_seconds", labels, 0))
                lines.append(metric_line("ping_probe_duration_seconds", labels, 0))
                lines.append(metric_line("ping_probe_last_probe_timestamp_seconds", labels, int(time.time())))
                continue

            if state.recent_successes:
                ratio = sum(state.recent_successes) / len(state.recent_successes)
                lines.append(metric_line("ping_probe_window_success_ratio", labels, f"{ratio:.6f}"))
            else:
                lines.append(metric_line("ping_probe_window_success_ratio", labels, 0))

            if result is None:
                lines.append(metric_line("ping_probe_success", labels, 0))
                lines.append(metric_line("ping_probe_last_probe_timestamp_seconds", labels, 0))
                continue

            lines.append(metric_line("ping_probe_success", labels, 1 if result.ok else 0))
            lines.append(metric_line("ping_probe_sent_packets", labels, result.sent))
            lines.append(metric_line("ping_probe_received_packets", labels, result.received))
            lines.append(metric_line("ping_probe_packet_loss_ratio", labels, f"{result.packet_loss_ratio:.6f}"))
            lines.append(metric_line("ping_probe_duration_seconds", labels, f"{result.duration_seconds:.6f}"))
            lines.append(metric_line("ping_probe_last_probe_timestamp_seconds", labels, int(result.timestamp)))
            if result.rtt_ms is not None:
                lines.append(metric_line("ping_probe_rtt_seconds", labels, f"{result.rtt_ms / 1000:.6f}"))
            if result.min_rtt_ms is not None:
                lines.append(metric_line("ping_probe_min_rtt_seconds", labels, f"{result.min_rtt_ms / 1000:.6f}"))
            if result.max_rtt_ms is not None:
                lines.append(metric_line("ping_probe_max_rtt_seconds", labels, f"{result.max_rtt_ms / 1000:.6f}"))

        return "\n".join(lines) + "\n"

    def render_targets(self) -> str:
        with self.lock:
            payload: list[dict[str, Any]] = []
            for state in self.states.values():
                result = state.last_result
                payload.append(
                    {
                        "target": state.config.target,
                        "labels": state.config.labels,
                        "enabled": state.config.enabled,
                        "comment": state.config.comment,
                        "reason": probe_reason(result, state.config.enabled, self.config.success_required),
                        "attempts_total": state.attempts_total,
                        "failures_total": state.failures_total,
                        "consecutive_failures": state.consecutive_failures,
                        "last_success_timestamp": state.last_success_timestamp,
                        "last_result": dataclasses.asdict(result) if result is not None else None,
                    }
                )
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


class ExporterHandler(BaseHTTPRequestHandler):
    exporter: PingExporter

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/metrics":
            self.write_response(200, "text/plain; version=0.0.4; charset=utf-8", self.exporter.render_metrics())
        elif path == "/healthz":
            self.write_response(200, "text/plain; charset=utf-8", "ok\n")
        elif path == "/targets":
            self.write_response(200, "application/json; charset=utf-8", self.exporter.render_targets())
        else:
            self.write_response(404, "text/plain; charset=utf-8", "not found\n")

    def write_response(self, status: int, content_type: str, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def run_server(config: ExporterConfig) -> None:
    host, port = parse_listen(config.listen)
    exporter = PingExporter(config)
    ExporterHandler.exporter = exporter
    server = ThreadingHTTPServer((host, port), ExporterHandler)

    def handle_stop(signum: int, frame: Any) -> None:
        logging.info("received signal %s, shutting down", signum)
        exporter.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)
    exporter.start()
    logging.info("multi_ping_exporter listening on %s:%s, targets=%s", host, port, len(config.targets))
    server.serve_forever()
    server.server_close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prometheus exporter for cached multi-target ping probes.")
    parser.add_argument("--config", default="multi_ping_exporter.json", help="Path to exporter JSON config.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    configure_logging(args.verbose)
    try:
        config = load_config(args.config)
        run_server(config)
    except Exception as exc:
        logging.error("startup failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
