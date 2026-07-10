#!/usr/bin/env python3
"""
BitB Daemon Module
Handles systemd service integration, daemonization, PID management,
graceful shutdown, and service health monitoring.
"""

import os
import sys
import time
import signal
import logging
import subprocess
import threading
from pathlib import Path
from typing import Optional, Callable, Dict, Any

log = logging.getLogger("bitb")

# ─── Paths ──────────────────────────────────────────────────────────────────
PID_FILE = Path("/var/run/bitb/bitb.pid")
SYSTEMD_SERVICE_PATH = Path("/etc/systemd/system/bitb.service")
LOG_DIR = Path("/var/log/bitb")
DATA_DIR = Path("/data/bitb")
RESTART_FLAG_FILE = DATA_DIR / ".restart_flag"


class DaemonManager:
    """
    Manages daemon lifecycle, PID file, signals, and systemd integration.
    
    Features:
    - PID file management (prevents multiple instances)
    - Graceful shutdown on SIGTERM/SIGINT
    - Automatic restart flag detection
    - Systemd notify support
    - Health check endpoint integration
    """
    
    def __init__(self, shutdown_callback: Optional[Callable] = None):
        self.shutdown_callback = shutdown_callback
        self._shutdown_event = threading.Event()
        self._shutdown_in_progress = False
        self._pid = os.getpid()
        self._is_systemd = self._detect_systemd()
        
        # Ensure directories exist
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    def _detect_systemd(self) -> bool:
        """Detect if we're running under systemd."""
        try:
            return (
                os.environ.get("INVOCATION_ID") is not None or
                os.environ.get("JOURNAL_STREAM") is not None or
                subprocess.run(
                    ["systemctl", "is-system-running"],
                    capture_output=True, timeout=5
                ).returncode == 0
            )
        except:
            return False
    
    # ─── PID File Management ──────────────────────────────────────────────
    
    def write_pid_file(self) -> bool:
        """Write the current PID to the PID file."""
        try:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(str(self._pid))
            log.info(f"📝 PID file written: {PID_FILE} ({self._pid})")
            return True
        except Exception as e:
            log.error(f"Failed to write PID file: {e}")
            return False
    
    def remove_pid_file(self):
        """Remove the PID file on shutdown."""
        try:
            if PID_FILE.exists():
                PID_FILE.unlink()
                log.info("🧹 PID file removed")
        except Exception as e:
            log.warning(f"Failed to remove PID file: {e}")
    
    def check_running(self) -> bool:
        """Check if another instance is already running."""
        if not PID_FILE.exists():
            return False
        
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if process exists
            os.kill(old_pid, 0)
            log.warning(f"⚠️  Another BitB instance is already running (PID: {old_pid})")
            return True
        except (ProcessLookupError, ValueError):
            # Stale PID file
            log.info("🧹 Removing stale PID file")
            PID_FILE.unlink(missing_ok=True)
            return False
        except Exception as e:
            log.warning(f"Could not check PID: {e}")
            return False
    
    # ─── Signal Handling ──────────────────────────────────────────────────
    
    def setup_signal_handlers(self):
        """Register signal handlers for graceful shutdown."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_signal)
        signal.signal(signal.SIGUSR1, self._handle_signal)
        
        log.info("🔔 Signal handlers registered (SIGTERM, SIGINT, SIGHUP, SIGUSR1)")
    
    def _handle_signal(self, signum: int, frame):
        """Handle incoming signals."""
        sig_name = signal.Signals(signum).name
        
        if signum == signal.SIGHUP:
            log.info(f"🔄 Received SIGHUP - reloading configuration")
            # Reload configuration (re-read config file, re-apply rules)
            if hasattr(self, '_on_reload') and self._on_reload:
                self._on_reload()
            return
        
        if signum == signal.SIGUSR1:
            log.info(f"📊 Received SIGUSR1 - dumping status")
            if hasattr(self, '_on_status') and self._on_status:
                self._on_status()
            return
        
        log.info(f"🛑 Received {sig_name}, initiating graceful shutdown...")
        self.initiate_shutdown()
    
    def set_reload_callback(self, callback: Callable):
        """Set callback for SIGHUP reload."""
        self._on_reload = callback
    
    def set_status_callback(self, callback: Callable):
        """Set callback for SIGUSR1 status dump."""
        self._on_status = callback
    
    def initiate_shutdown(self):
        """Begin graceful shutdown procedure."""
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        self._shutdown_event.set()
        
        log.info("🛑 Graceful shutdown initiated...")
        
        # Call the shutdown callback
        if self.shutdown_callback:
            try:
                self.shutdown_callback()
            except Exception as e:
                log.error(f"Shutdown callback error: {e}")
        
        # Remove PID file
        self.remove_pid_file()
        
        # Notify systemd
        self.notify_systemd("STOPPING=1")
        
        log.info("👋 BitB shutdown complete")
    
    def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        """Wait for shutdown signal. Returns True if shutdown requested."""
        return self._shutdown_event.wait(timeout=timeout)
    
    def is_shutdown_requested(self) -> bool:
        """Check if shutdown has been requested."""
        return self._shutdown_event.is_set()
    
    # ─── Systemd Integration ──────────────────────────────────────────────
    
    def notify_systemd(self, state: str):
        """Send a notification to systemd via sd_notify."""
        if not self._is_systemd:
            return
        
        try:
            # Use systemd's sd_notify if available
            import ctypes
            libsystemd = ctypes.CDLL("libsystemd.so.0", use_errno=True)
            
            msg = state.encode()
            libsystemd.sd_notify(0, msg)
        except Exception:
            # Fallback: write to NOTIFY_SOCKET directly
            sock_path = os.environ.get("NOTIFY_SOCKET")
            if sock_path:
                try:
                    import socket as sock_module
                    addr = f"\0{sock_path[1:]}" if sock_path.startswith("@") else sock_path
                    s = sock_module.socket(sock_module.AF_UNIX, sock_module.SOCK_DGRAM)
                    s.connect(addr)
                    s.sendall(state.encode())
                    s.close()
                except Exception:
                    pass
    
    def systemd_ready(self):
        """Notify systemd that the service is ready."""
        self.notify_systemd("READY=1")
        log.info("✅ Systemd readiness notification sent")
    
    def systemd_reloading(self):
        """Notify systemd that we're reloading."""
        self.notify_systemd("RELOADING=1")
    
    def systemd_watchdog(self):
        """Send watchdog ping to systemd."""
        self.notify_systemd("WATCHDOG=1")
    
    # ─── Restart Management ───────────────────────────────────────────────
    
    def set_restart_flag(self):
        """Set a flag that indicates we should restart after shutdown."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            RESTART_FLAG_FILE.write_text(str(time.time()))
            log.info("🔄 Restart flag set (will restart after shutdown)")
        except Exception as e:
            log.warning(f"Could not set restart flag: {e}")
    
    def clear_restart_flag(self):
        """Clear the restart flag."""
        try:
            RESTART_FLAG_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    
    def should_restart(self) -> bool:
        """Check if we should restart after shutdown."""
        return RESTART_FLAG_FILE.exists()
    
    # ─── Service Status ──────────────────────────────────────────────────
    
    def get_status(self) -> Dict[str, Any]:
        """Get daemon status for API/heartbeat."""
        return {
            "pid": self._pid,
            "running": True,
            "uptime": time.time() - self._start_time if hasattr(self, '_start_time') else 0,
            "systemd": self._is_systemd,
            "pid_file": str(PID_FILE),
            "shutdown_requested": self._shutdown_event.is_set(),
            "restart_pending": self.should_restart(),
        }
    
    def set_start_time(self):
        """Record the start time."""
        self._start_time = time.time()


# ═══════════════════════════════════════════════════════════════════════════
# Systemd Service File Generator
# ═══════════════════════════════════════════════════════════════════════════

SYSTEMD_SERVICE_TEMPLATE = """[Unit]
Description=BitB MFA Bypass Framework v2.0
Documentation=https://github.com/bitb-framework
After=network.target docker.service
Wants=docker.service
Requires=docker.service

[Service]
Type=notify
ExecStartPre=/usr/bin/env bash -c 'while ! docker info >/dev/null 2>&1; do sleep 1; done'
ExecStart=/usr/local/bin/bitb --daemon
ExecReload=/bin/kill -HUP $MAINPID
ExecStop=/bin/kill -TERM $MAINPID
Restart=on-failure
RestartSec=10
TimeoutStartSec=120
TimeoutStopSec=30

# User and group
User=root
Group=root

# Capabilities for iptables
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_SYS_ADMIN
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW

# Security
NoNewPrivileges=false
ProtectSystem=full
ProtectHome=false
PrivateTmp=false

# Resource limits
LimitNOFILE=65536
LimitNPROC=4096

# Process management
KillMode=process
SendSIGKILL=no

# Notify systemd
WatchdogSec=30
NotifyAccess=all

# Environment
Environment=BITB_HOME=/data/bitb
Environment=BITB_CONFIG=/data/bitb/config.json
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target
"""


def install_systemd_service() -> bool:
    """
    Install the BitB systemd service file.
    Must be run as root.
    """
    try:
        SYSTEMD_SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
        SYSTEMD_SERVICE_PATH.write_text(SYSTEMD_SERVICE_TEMPLATE)
        
        # Reload systemd
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=10)
        log.info(f"✅ Systemd service installed at {SYSTEMD_SERVICE_PATH}")
        
        print(f"""
╔══════════════════════════════════════════════════════════════╗
║              BitB Systemd Service Installed                  ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  Manage the service with:                                    ║
║                                                              ║
║    sudo systemctl enable bitb    # Auto-start on boot        ║
║    sudo systemctl start bitb     # Start now                 ║
║    sudo systemctl stop bitb      # Stop                      ║
║    sudo systemctl restart bitb   # Restart                   ║
║    sudo systemctl status bitb    # Check status              ║
║    sudo journalctl -u bitb -f    # Follow logs               ║
║                                                              ║
╠══════════════════════════════════════════════════════════════╣
║  BitB binary should be at: /usr/local/bin/bitb               ║
║  Create symlink: sudo ln -sf $(pwd)/bitb.py /usr/local/bin/bitb  ║
╚══════════════════════════════════════════════════════════════╝
        """)
        return True
    except Exception as e:
        log.error(f"Failed to install systemd service: {e}")
        return False


def uninstall_systemd_service() -> bool:
    """Remove the BitB systemd service."""
    try:
        subprocess.run(["systemctl", "stop", "bitb"], capture_output=True, timeout=10)
        subprocess.run(["systemctl", "disable", "bitb"], capture_output=True, timeout=10)
        SYSTEMD_SERVICE_PATH.unlink(missing_ok=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=10)
        log.info("✅ Systemd service uninstalled")
        return True
    except Exception as e:
        log.error(f"Failed to uninstall systemd service: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════════
# CLI Entry Points
# ═══════════════════════════════════════════════════════════════════════════

def cmd_install():
    """CLI handler for --install command."""
    if os.geteuid() != 0:
        print("❌ Must be run as root to install systemd service")
        sys.exit(1)
    
    # Create symlink to /usr/local/bin
    script_path = Path(__file__).resolve()
    target = Path("/usr/local/bin/bitb")
    if not target.exists():
        try:
            target.symlink_to(script_path)
            print(f"✅ Symlink created: {target} -> {script_path}")
        except Exception as e:
            print(f"⚠️  Could not create symlink: {e}")
            print(f"   Manually: sudo ln -sf {script_path} /usr/local/bin/bitb")
    
    install_systemd_service()


def cmd_uninstall():
    """CLI handler for --uninstall command."""
    if os.geteuid() != 0:
        print("❌ Must be run as root to uninstall systemd service")
        sys.exit(1)
    
    uninstall_systemd_service()
    target = Path("/usr/local/bin/bitb")
    if target.exists():
        target.unlink()
        print("✅ Symlink removed")


def cmd_status():
    """CLI handler for --status command."""
    try:
        result = subprocess.run(
            ["systemctl", "status", "bitb", "--no-pager"],
            capture_output=True, text=True, timeout=10
        )
        print(result.stdout)
        if result.stderr:
            print(result.stderr)
    except Exception as e:
        print(f"Could not check service status: {e}")
    
    # Also check PID file
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            print(f"📝 PID file: {PID_FILE} (PID: {pid})")
        except:
            print(f"📝 PID file: {PID_FILE} (stale)")
    else:
        print("📝 PID file: Not running")


def cmd_enable():
    """CLI handler for --enable command."""
    if os.geteuid() != 0:
        print("❌ Must be run as root")
        sys.exit(1)
    subprocess.run(["systemctl", "enable", "bitb"], timeout=10)
    print("✅ BitB service enabled to start on boot")


def cmd_disable():
    """CLI handler for --disable command."""
    if os.geteuid() != 0:
        print("❌ Must be run as root")
        sys.exit(1)
    subprocess.run(["systemctl", "disable", "bitb"], timeout=10)
    print("✅ BitB service disabled from auto-start")
