#!/usr/bin/env python3
"""
Unified CLI launcher for the anomaly detection PoC.

Resets state, verifies infrastructure, and opens a tmux session with three
panes: ingestor, detector, and generator — so the entire demo runs from a
single command.

Usage:
    python run.py              # reset + launch
    python run.py --no-reset   # launch without resetting state
"""

import argparse
import os
import shlex
import subprocess
import time

POC_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_ACTIVATE = os.path.join(POC_DIR, ".venv", "bin", "activate")
SESSION_NAME = "anomaly-poc"

_QUOTED_POC_DIR = shlex.quote(POC_DIR)
_QUOTED_VENV_ACTIVATE = shlex.quote(VENV_ACTIVATE)


def run_command(command: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, shell=True, cwd=POC_DIR, check=check, capture_output=True, text=True)


def tmux_session_exists() -> bool:
    result = run_command(f"tmux has-session -t {SESSION_NAME} 2>/dev/null", check=False)
    return result.returncode == 0


def kill_existing_session() -> None:
    if tmux_session_exists():
        print(f"Killing existing tmux session '{SESSION_NAME}'...")
        run_command(f"tmux kill-session -t {SESSION_NAME}")


def wait_for_healthy_containers(timeout: int = 60) -> None:
    """Wait until all docker compose services report healthy."""
    print("Waiting for infrastructure to be healthy...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = run_command("docker compose ps --format '{{.Service}} {{.Status}}'", check=False)
        lines = [line for line in result.stdout.strip().split("\n") if line]
        if lines and all("healthy" in line.lower() for line in lines):
            print(f"All {len(lines)} services healthy.")
            return
        time.sleep(2)
    print("WARNING: Not all services healthy after timeout. Proceeding anyway.")


def ensure_infrastructure() -> None:
    """Start docker compose if not already running, and wait for healthy."""
    result = run_command("docker compose ps -q", check=False)
    if not result.stdout.strip():
        print("Starting infrastructure with docker compose...")
        run_command("docker compose up -d")
    wait_for_healthy_containers()


def reset_state() -> None:
    """Run reset.py to clear all state."""
    print("Resetting all state...")
    result = run_command(f"source {_QUOTED_VENV_ACTIVATE} && python reset.py --force", check=False)
    if result.returncode != 0:
        print(f"Reset output: {result.stderr or result.stdout}")
    else:
        print("State reset complete.")


def shell_command(module: str) -> str:
    """Build the shell command to run a Python module inside the venv."""
    return f"source {_QUOTED_VENV_ACTIVATE} && cd {_QUOTED_POC_DIR} && python -m {module}"


def create_tmux_panes() -> None:
    """Create the tmux session with three panes for ingestor, detector, and generator."""
    ingestor_command = shell_command("ingestion.ingestor")
    detector_command = shell_command("detection.detector")
    generator_command = shell_command("data.generator")
    run_command(
        f"tmux new-session -d -s {SESSION_NAME} -n demo "
        f"'{ingestor_command}; read -p \"[ingestor exited] press enter to close\"'"
    )
    run_command(
        f"tmux split-window -h -t {SESSION_NAME}:demo "
        f"'{detector_command}; read -p \"[detector exited] press enter to close\"'"
    )
    run_command(
        f"tmux split-window -v -t {SESSION_NAME}:demo.0 -l 30% "
        f"'{generator_command}; read -p \"[generator exited] press enter to close\"'"
    )


def configure_tmux_labels() -> None:
    """Set pane titles and border formatting for the tmux session."""
    # After splits, pane order is: 0=ingestor (top-left), 1=generator (bottom-left), 2=detector (right)
    run_command(f"tmux select-pane -t {SESSION_NAME}:demo.0 -T 'INGESTOR'")
    run_command(f"tmux select-pane -t {SESSION_NAME}:demo.1 -T 'GENERATOR'")
    run_command(f"tmux select-pane -t {SESSION_NAME}:demo.2 -T 'DETECTOR'")
    run_command(f"tmux set-option -t {SESSION_NAME} pane-border-status top")
    run_command(f"tmux set-option -t {SESSION_NAME} pane-border-format ' #{{pane_title}} '")
    run_command(f"tmux select-pane -t {SESSION_NAME}:demo.2")


def launch_tmux() -> None:
    """Create a tmux session with three panes and attach."""
    kill_existing_session()
    create_tmux_panes()
    configure_tmux_labels()
    print(f"\nAttaching to tmux session '{SESSION_NAME}'...")
    print("  Pane layout: INGESTOR (top-left) | DETECTOR (right) | GENERATOR (bottom-left)")
    print(f"  To detach: Ctrl+B, then D")
    print(f"  To stop: tmux kill-session -t {SESSION_NAME}")
    print()
    os.execvp("tmux", ["tmux", "attach-session", "-t", SESSION_NAME])


def main() -> None:
    parser = argparse.ArgumentParser(description="Launch the anomaly detection PoC demo")
    parser.add_argument("--no-reset", action="store_true", help="Skip state reset")
    arguments = parser.parse_args()

    ensure_infrastructure()

    if not arguments.no_reset:
        reset_state()

    launch_tmux()


if __name__ == "__main__":
    main()
