#!/usr/bin/env python3
"""
BitB Extension Manager
Handles deployment and injection of browser extensions into Firefox containers.
"""

import os
import json
import shutil
import logging
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger("bitb")

# Path where extensions are stored on the host
EXTENSIONS_DIR = Path(__file__).parent / "extensions"
BUILD_DIR = EXTENSIONS_DIR / "build"


class ExtensionManager:
    """Manages building and injecting Firefox extensions into BitB containers."""
    
    def __init__(self):
        self.available_extensions = self._discover_extensions()
        log.info(f"Extension Manager initialized with {len(self.available_extensions)} extensions: {list(self.available_extensions.keys())}")
    
    def _discover_extensions(self) -> Dict[str, Path]:
        """Discover available extensions in the extensions directory."""
        extensions = {}
        
        # Look for built XPI files first
        if BUILD_DIR.exists():
            for xpi in BUILD_DIR.glob("*.xpi"):
                name = xpi.stem
                extensions[name] = xpi
        
        # Also look for unpacked extension directories
        for ext_dir in EXTENSIONS_DIR.iterdir():
            if ext_dir.is_dir() and (ext_dir / "manifest.json").exists():
                name = ext_dir.name
                if name not in extensions:
                    extensions[name] = ext_dir
        
        return extensions
    
    def build_all(self) -> Dict[str, Path]:
        """Build all extensions into XPI files."""
        log.info("Building all extensions...")
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        
        built = {}
        for ext_name, ext_path in self.available_extensions.items():
            if ext_path.is_dir():
                xpi_path = self._build_xpi(ext_name, ext_path)
                if xpi_path:
                    built[ext_name] = xpi_path
            else:
                # Already an XPI
                built[ext_name] = ext_path
        
        log.info(f"Built {len(built)} extensions: {list(built.keys())}")
        return built
    
    def _build_xpi(self, name: str, source_dir: Path) -> Optional[Path]:
        """Build a single XPI from an extension directory."""
        xpi_path = BUILD_DIR / f"{name}.xpi"
        
        try:
            with zipfile.ZipFile(xpi_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path in source_dir.rglob("*"):
                    if file_path.is_file() and file_path.name != ".DS_Store":
                        arcname = str(file_path.relative_to(source_dir))
                        zf.write(file_path, arcname)
            
            log.info(f"  Built: {name} -> {xpi_path}")
            return xpi_path
        except Exception as e:
            log.error(f"  Failed to build {name}: {e}")
            return None
    
    def get_extension_paths(self) -> List[str]:
        """Get list of XPI paths to inject into containers."""
        paths = []
        
        # Build if needed
        self.build_all()
        
        if BUILD_DIR.exists():
            for xpi in BUILD_DIR.glob("*.xpi"):
                paths.append(str(xpi.absolute()))
        
        return paths
    
    def generate_policies_json(self, xpi_paths: List[str]) -> str:
        """Generate Firefox policies.json to force-install extensions."""
        # In the container, extensions will be mounted to /extensions/
        # We reference them by their mounted path
        install_list = []
        for path in xpi_paths:
            xpi_name = Path(path).name
            container_path = f"/extensions/{xpi_name}"
            install_list.append(container_path)
        
        policies = {
            "policies": {
                "Extensions": {
                    "Install": install_list,
                    "Locked": [xpi_name.replace(".xpi", "") for xpi_name in 
                               [Path(p).name for p in xpi_paths]]
                },
                "Certificates": {
                    "Install": [],
                    "ImportEnterpriseRoots": True
                },
                "Security": {
                    "EnableCSPLogging": False
                }
            }
        }
        
        # Also add preferences for development mode
        return json.dumps(policies, indent=2)
    
    def generate_prefs_js(self) -> str:
        """Generate user preferences for Firefox to allow unsigned extensions."""
        prefs = """// BitB Firefox Preferences
// Allow unsigned extensions for development/assessment purposes
user_pref("xpinstall.signatures.required", false);
user_pref("xpinstall.whitelist.required", false);
user_pref("extensions.autoDisableScopes", 0);
user_pref("extensions.enabledScopes", 15);
user_pref("extensions.installDistroAddons", true);
user_pref("extensions.systemAddon.update.enabled", false);
user_pref("extensions.update.enabled", false);
user_pref("extensions.getAddons.cache.enabled", false);
user_pref("extensions.webservice.discoverURL", "");
user_pref("extensions.langpacks.signatures.required", false);

// Disable storage access checks for extension exfil
user_pref("security.fileuri.strict_origin_policy", false);
user_pref("network.http.allow-headers", "*");

// Allow localhost connections from extensions
user_pref("network.websocket.allowInsecureFromHTTPS", true);
user_pref("network.websocket.allowInsecureFromHTTP", true);

// Performance tweaks
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.tabs.warnOnClose", false);
user_pref("browser.tabs.warnOnCloseOtherTabs", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("browser.startup.page", 3);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("toolkit.telemetry.updatePing.enabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("browser.laterrun.enabled", false);
user_pref("dom.webnotifications.enabled", false);
user_pref("dom.push.enabled", false);
user_pref("browser.safebrowsing.enabled", false);
user_pref("browser.safebrowsing.malware.enabled", false);
user_pref("signon.rememberSignons", true);
user_pref("signon.autofillForms", true);
user_pref("signon.storeWhenAutocompleteOff", true);
user_pref("network.cookie.lifetimePolicy", 0);
user_pref("network.cookie.cookieBehavior", 0);
user_pref("security.csp.enable", false);
"""
        return prefs
