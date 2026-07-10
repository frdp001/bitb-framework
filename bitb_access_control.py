#!/usr/bin/env python3
"""
BitB Access Control Module
IP-based whitelist/blacklist for VNC session access.
Uses iptables rules to dynamically allow/block IPs from reaching session ports.
"""

import os
import re
import json
import time
import logging
import subprocess
import threading
import ipaddress
from pathlib import Path
from typing import Set, List, Optional, Dict

log = logging.getLogger("bitb")

ACCESS_CONTROL_DIR = Path("/data/bitb/access_control")
WHITELIST_FILE = ACCESS_CONTROL_DIR / "whitelist.txt"
BLACKLIST_FILE = ACCESS_CONTROL_DIR / "blacklist.txt"
SESSION_ACCESS_FILE = ACCESS_CONTROL_DIR / "session_access.json"


class IPAccessController:
    """
    Manages IP-based access control for BitB VNC sessions using iptables.
    
    Features:
    - Global whitelist/blacklist (applied to all sessions)
    - Per-session IP allow/deny lists
    - Automatic iptables rule injection
    - Rule persistence across service restarts
    - Web dashboard integration
    """
    
    def __init__(self, chain_prefix: str = "BITB"):
        self.chain_prefix = chain_prefix
        self.global_whitelist: Set[str] = set()
        self.global_blacklist: Set[str] = set()
        self.session_access: Dict[str, Dict] = {}  # session_id -> {whitelist, blacklist, mode}
        self.lock = threading.Lock()
        self._enabled = False
        self._initialized = False
        
        ACCESS_CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        
        # Load persisted rules
        self._load_rules()
    
    # ─── Initialization ────────────────────────────────────────────────────
    
    def initialize(self):
        """Initialize iptables chains. Must be run as root."""
        if self._initialized:
            return
        
        try:
            # Create the main BITB chain if it doesn't exist
            self._iptables("-N", self.chain_prefix, check=False)
            self._iptables("-N", f"{self.chain_prefix}_WHITELIST", check=False)
            self._iptables("-N", f"{self.chain_prefix}_BLACKLIST", check=False)
            
            # Insert into INPUT chain (at the top for early filtering)
            self._iptables("-I", "INPUT", "1", "-j", self.chain_prefix, check=False)
            
            # Default: jump to whitelist check
            self._iptables("-A", self.chain_prefix, "-j", f"{self.chain_prefix}_WHITELIST", check=False)
            
            self._enabled = True
            self._initialized = True
            log.info("✅ IP access control initialized with iptables chains")
            
            # Apply persisted rules
            self._apply_all_rules()
            
        except Exception as e:
            log.warning(f"⚠️  Could not initialize iptables (run as root?): {e}")
            log.warning("   IP access control will use software-level filtering only")
            self._enabled = False
            self._initialized = True
    
    def cleanup(self):
        """Remove iptables chains and rules."""
        if not self._initialized:
            return
        
        try:
            # Flush and delete chains
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
    
    # ─── IP Validation ─────────────────────────────────────────────────────
    
    def _validate_ip(self, ip_str: str) -> bool:
        """Validate an IP address or CIDR notation."""
        try:
            if "/" in ip_str:
                ipaddress.ip_network(ip_str, strict=False)
            else:
                ipaddress.ip_address(ip_str)
            return True
        except ValueError:
            return False
    
    def _validate_port(self, port: int) -> bool:
        """Validate a port number."""
        return 1 <= port <= 65535
    
    # ─── Global Lists ──────────────────────────────────────────────────────
    
    def add_to_whitelist(self, ip_or_cidr: str) -> bool:
        """Add an IP/CIDR to the global whitelist."""
        if not self._validate_ip(ip_or_cidr):
            log.error(f"Invalid IP/CIDR: {ip_or_cidr}")
            return False
        
        with self.lock:
            self.global_whitelist.add(ip_or_cidr)
            self._save_rules()
            self._apply_whitelist_rules()
        
        log.info(f"➕ Added to global whitelist: {ip_or_cidr}")
        return True
    
    def remove_from_whitelist(self, ip_or_cidr: str) -> bool:
        """Remove an IP/CIDR from the global whitelist."""
        with self.lock:
            if ip_or_cidr in self.global_whitelist:
                self.global_whitelist.discard(ip_or_cidr)
                self._save_rules()
                self._apply_whitelist_rules()
                log.info(f"➖ Removed from global whitelist: {ip_or_cidr}")
                return True
        return False
    
    def add_to_blacklist(self, ip_or_cidr: str) -> bool:
        """Add an IP/CIDR to the global blacklist."""
        if not self._validate_ip(ip_or_cidr):
            return False
        
        with self.lock:
            self.global_blacklist.add(ip_or_cidr)
            self._save_rules()
            self._apply_blacklist_rules()
        
        log.info(f"🚫 Added to global blacklist: {ip_or_cidr}")
        return True
    
    def remove_from_blacklist(self, ip_or_cidr: str) -> bool:
        """Remove an IP/CIDR from the global blacklist."""
        with self.lock:
            if ip_or_cidr in self.global_blacklist:
                self.global_blacklist.discard(ip_or_cidr)
                self._save_rules()
                self._apply_blacklist_rules()
                return True
        return False
    
    # ─── Per-Session Access ────────────────────────────────────────────────
    
    def set_session_access(self, session_id: str, 
                          whitelist: Optional[List[str]] = None,
                          blacklist: Optional[List[str]] = None,
                          mode: str = "whitelist") -> bool:
        """
        Set per-session access rules.
        
        Args:
            session_id: The session to configure
            whitelist: List of IPs/CIDRs to allow
            blacklist: List of IPs/CIDRs to deny
            mode: 'whitelist' (default deny) or 'blacklist' (default allow)
        """
        if mode not in ("whitelist", "blacklist"):
            log.error(f"Invalid mode: {mode}. Use 'whitelist' or 'blacklist'")
            return False
        
        # Validate IPs
        for ip_list, list_name in [(whitelist or [], "whitelist"), (blacklist or [], "blacklist")]:
            for ip in ip_list:
                if not self._validate_ip(ip):
                    log.error(f"Invalid IP in {list_name}: {ip}")
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
        """Remove per-session access rules."""
        with self.lock:
            if session_id in self.session_access:
                del self.session_access[session_id]
                self._save_rules()
                log.info(f"🔓 Removed access rules for session {session_id[:16]}")
    
    def check_session_access(self, session_id: str, client_ip: str, port: int) -> bool:
        """
        Check if a client IP is allowed to access a session.
        Returns True if allowed, False if denied.
        """
        # First check global blacklist (hard deny)
        for cidr in self.global_blacklist:
            if self._ip_in_cidr(client_ip, cidr):
                log.info(f"🚫 DENIED (global blacklist): {client_ip} -> {session_id[:16]}")
                return False
        
        # Then check global whitelist (hard allow)
        for cidr in self.global_whitelist:
            if self._ip_in_cidr(client_ip, cidr):
                return True
        
        # Then check per-session rules
        with self.lock:
            rules = self.session_access.get(session_id)
        
        if rules:
            if rules["mode"] == "whitelist":
                # Default deny - only allow whitelisted IPs
                for cidr in rules["whitelist"]:
                    if self._ip_in_cidr(client_ip, cidr):
                        return True
                log.info(f"🚫 DENIED (session whitelist): {client_ip} -> {session_id[:16]}")
                return False
            else:
                # Default allow - only deny blacklisted IPs
                for cidr in rules["blacklist"]:
                    if self._ip_in_cidr(client_ip, cidr):
                        log.info(f"🚫 DENIED (session blacklist): {client_ip} -> {session_id[:16]}")
                        return False
                return True
        
        # No per-session rules, check if global whitelist has entries
        # If global whitelist has entries and client isn't in it, deny
        if self.global_whitelist:
            log.info(f"🚫 DENIED (default): {client_ip} -> {session_id[:16]}")
            return False
        
        # No restrictions - allow
        return True
    
    def _ip_in_cidr(self, ip: str, cidr: str) -> bool:
        """Check if an IP falls within a CIDR range."""
        try:
            addr = ipaddress.ip_address(ip)
            if "/" in cidr:
                network = ipaddress.ip_network(cidr, strict=False)
                return addr in network
            else:
                target = ipaddress.ip_address(cidr)
                return addr == target
        except ValueError:
            return False
    
    # ─── iptables Rules ────────────────────────────────────────────────────
    
    def _iptables(self, *args, check: bool = True) -> bool:
        """Execute an iptables command."""
        try:
            cmd = ["iptables"] + list(args)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode != 0 and check:
                log.warning(f"iptables {' '.join(args)} failed: {result.stderr.strip()}")
                return False
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            log.warning(f"iptables command timed out: {' '.join(args)}")
            return False
        except FileNotFoundError:
            return False
        except Exception as e:
            log.debug(f"iptables error: {e}")
            return False
    
    def _apply_whitelist_rules(self):
        """Apply global whitelist rules to iptables."""
        if not self._enabled:
            return
        
        # Flush the whitelist chain
        self._iptables("-F", f"{self.chain_prefix}_WHITELIST")
        
        # Add allow rules for each whitelisted IP
        for cidr in self.global_whitelist:
            self._iptables("-A", f"{self.chain_prefix}_WHITELIST", 
                          "-s", cidr, "-j", "RETURN")
        
        # If whitelist is non-empty, deny everything else
        if self.global_whitelist:
            # Log denied packets
            self._iptables("-A", f"{self.chain_prefix}_WHITELIST", 
                          "-m", "limit", "--limit", "5/min", "-j", "LOG",
                          "--log-prefix", "BITB-DENIED: ")
            self._iptables("-A", f"{self.chain_prefix}_WHITELIST", "-j", "DROP")
    
    def _apply_blacklist_rules(self):
        """Apply global blacklist rules to iptables."""
        if not self._enabled:
            return
        
        # Flush the blacklist chain
        self._iptables("-F", f"{self.chain_prefix}_BLACKLIST")
        
        # Add deny rules for each blacklisted IP
        for cidr in self.global_blacklist:
            self._iptables("-A", f"{self.chain_prefix}_BLACKLIST",
                          "-s", cidr, "-j", "DROP")
        
        # Allow everything else
        self._iptables("-A", f"{self.chain_prefix}_BLACKLIST", "-j", "RETURN")
    
    def _apply_all_rules(self):
        """Apply all current rules."""
        if not self._enabled:
            return
        self._apply_whitelist_rules()
        self._apply_blacklist_rules()
    
    # ─── Persistence ───────────────────────────────────────────────────────
    
    def _save_rules(self):
        """Persist rules to disk."""
        try:
            data = {
                "global_whitelist": sorted(self.global_whitelist),
                "global_blacklist": sorted(self.global_blacklist),
                "session_access": {
                    sid: {
                        "whitelist": sorted(rules["whitelist"]),
                        "blacklist": sorted(rules["blacklist"]),
                        "mode": rules["mode"],
                        "updated_at": rules.get("updated_at", time.time())
                    }
                    for sid, rules in self.session_access.items()
                },
                "updated_at": time.time()
            }
            
            SESSION_ACCESS_FILE.write_text(json.dumps(data, indent=2))
            
        except Exception as e:
            log.error(f"Failed to save access rules: {e}")
    
    def _load_rules(self):
        """Load persisted rules from disk."""
        try:
            if SESSION_ACCESS_FILE.exists():
                data = json.loads(SESSION_ACCESS_FILE.read_text())
                self.global_whitelist = set(data.get("global_whitelist", []))
                self.global_blacklist = set(data.get("global_blacklist", []))
                
                for sid, rules in data.get("session_access", {}).items():
                    self.session_access[sid] = {
                        "whitelist": set(rules.get("whitelist", [])),
                        "blacklist": set(rules.get("blacklist", [])),
                        "mode": rules.get("mode", "whitelist"),
                        "updated_at": rules.get("updated_at", time.time())
                    }
                
                log.info(f"📂 Loaded access rules: {len(self.global_whitelist)} whitelist, "
                        f"{len(self.global_blacklist)} blacklist, "
                        f"{len(self.session_access)} session rules")
        except Exception as e:
            log.warning(f"Could not load access rules: {e}")
    
    # ─── API Helpers ───────────────────────────────────────────────────────
    
    def get_status(self) -> Dict:
        """Get current access control status for API."""
        with self.lock:
            return {
                "enabled": self._enabled,
                "initialized": self._initialized,
                "global_whitelist": sorted(self.global_whitelist),
                "global_blacklist": sorted(self.global_blacklist),
                "session_access": {
                    sid: {
                        "whitelist": sorted(rules["whitelist"]),
                        "blacklist": sorted(rules["blacklist"]),
                        "mode": rules["mode"]
                    }
                    for sid, rules in self.session_access.items()
                },
                "active_iptables_rules": self._count_iptables_rules()
            }
    
    def _count_iptables_rules(self) -> Dict:
        """Count active iptables rules."""
        counts = {"whitelist": 0, "blacklist": 0, "main": 0}
        try:
            for chain, name in [(f"{self.chain_prefix}_WHITELIST", "whitelist"),
                               (f"{self.chain_prefix}_BLACKLIST", "blacklist"),
                               (self.chain_prefix, "main")]:
                result = subprocess.run(
                    ["iptables", "-L", chain, "-n", "--line-numbers"],
                    capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    lines = [l for l in result.stdout.split("\n") 
                            if l.strip() and not l.startswith("Chain") and "target" not in l.lower()]
                    counts[name] = len(lines)
        except:
            pass
        return counts
