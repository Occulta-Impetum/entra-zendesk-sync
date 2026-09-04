#!/usr/bin/env python3
"""Graphical configuration wizard for Entra -> Zendesk Sync."""

from __future__ import annotations

import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.config import (
    ConfigError,
    build_config,
    existing_mapping_by_group_id,
    load_config,
    save_config,
)
from lib.graph import GraphError, get_graph_access_token, get_security_groups, load_graph_config
from lib.zendesk import ZendeskError, get_access_token, get_organizations, load_zendesk_config


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window_id, width=event.width))
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event: tk.Event) -> None:
        if self.winfo_ismapped():
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class ConfigureApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Entra -> Zendesk Sync Configuration")
        self.geometry("860x650")
        self.minsize(720, 520)
        self.graph_config: dict[str, str] = {}
        self.zendesk_config: dict[str, str] = {}
        self.groups: list[dict] = []
        self.organizations: list[dict] = []
        self.existing_config: dict = {}
        self.existing_mappings: dict[str, dict] = {}
        self.selected_group_ids: set[str] = set()
        self.group_vars: dict[str, tk.BooleanVar] = {}
        self.mapping_vars: dict[str, tk.StringVar] = {}
        self.saved_config_path: Path | None = None
        self.container = ttk.Frame(self, padding=16)
        self.container.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="Starting configuration wizard...")
        self._show_loading_page()
        self.after(100, self._begin_discovery)

    def _clear(self) -> None:
        for widget in self.container.winfo_children():
            widget.destroy()

    def _show_loading_page(self) -> None:
        self._clear()
        ttk.Label(self.container, text="Entra -> Zendesk Sync Configuration", font=("Segoe UI", 16, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(self.container, text="Connecting to Microsoft Entra and Zendesk. This may take a moment.").pack(anchor="w", pady=(0, 14))
        progress = ttk.Progressbar(self.container, mode="indeterminate")
        progress.pack(fill="x", pady=(0, 14))
        progress.start(10)
        ttk.Label(self.container, textvariable=self.status_var).pack(anchor="w")

    def _begin_discovery(self) -> None:
        threading.Thread(target=self._discover_data, daemon=True).start()

    def _set_status(self, text: str) -> None:
        self.after(0, self.status_var.set, text)

    def _discover_data(self) -> None:
        try:
            self._set_status("Loading existing configuration...")
            self.existing_config = load_config()
            self.existing_mappings = existing_mapping_by_group_id(self.existing_config)
            self._set_status("Authenticating to Microsoft Graph...")
            self.graph_config = load_graph_config()
            graph_token = get_graph_access_token(self.graph_config)
            self._set_status("Loading Entra security groups...")
            self.groups = get_security_groups(graph_token)
            self._set_status("Authenticating to Zendesk...")
            self.zendesk_config = load_zendesk_config()
            zendesk_token, _ = get_access_token(self.zendesk_config)
            self._set_status("Loading Zendesk organizations...")
            self.organizations = get_organizations(zendesk_token, self.zendesk_config["subdomain"])
            self.selected_group_ids = {
                group_id for group_id in self.existing_mappings
                if any(str(group.get("id")) == group_id for group in self.groups)
            }
            self.after(0, self._show_group_page)
        except (GraphError, ZendeskError, ConfigError, Exception) as exc:
            self.after(0, self._show_error, str(exc))

    def _show_error(self, detail: str) -> None:
        self._clear()
        ttk.Label(self.container, text="Configuration could not be loaded", font=("Segoe UI", 15, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(self.container, text=detail, wraplength=780).pack(anchor="w", pady=(0, 14))
        ttk.Button(self.container, text="Close", command=self.destroy).pack(anchor="e")

    def _show_group_page(self) -> None:
        self._clear()
        ttk.Label(self.container, text="1. Select Entra Security Groups", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self.container, text="Choose the Entra groups that define Zendesk provisioning scope. Existing selections are checked automatically.", wraplength=800).pack(anchor="w", pady=(4, 12))
        search_frame = ttk.Frame(self.container)
        search_frame.pack(fill="x", pady=(0, 8))
        ttk.Label(search_frame, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.pack(side="left", fill="x", expand=True, padx=(8, 8))
        ttk.Label(search_frame, text=f"{len(self.groups)} groups loaded").pack(side="right")
        self.search_var.trace_add("write", lambda *_args: self._render_group_list())
        self.group_scroll = ScrollableFrame(self.container)
        self.group_scroll.pack(fill="both", expand=True, pady=(0, 10))
        action_frame = ttk.Frame(self.container)
        action_frame.pack(fill="x")
        ttk.Button(action_frame, text="Select All Visible", command=self._select_all_visible).pack(side="left")
        ttk.Button(action_frame, text="Clear Visible", command=self._clear_visible).pack(side="left", padx=(8, 0))
        self.selection_count_var = tk.StringVar()
        ttk.Label(action_frame, textvariable=self.selection_count_var).pack(side="left", padx=16)
        ttk.Button(action_frame, text="Next: Map Organizations >", command=self._go_to_mapping).pack(side="right")
        self._render_group_list()
        search_entry.focus_set()

    def _visible_groups(self) -> list[dict]:
        query = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        if not query:
            return self.groups
        return [group for group in self.groups if query in str(group.get("displayName") or "").lower() or query in str(group.get("description") or "").lower()]

    def _ensure_group_var(self, group_id: str) -> tk.BooleanVar:
        if group_id not in self.group_vars:
            self.group_vars[group_id] = tk.BooleanVar(value=group_id in self.selected_group_ids)
        return self.group_vars[group_id]

    def _render_group_list(self) -> None:
        for widget in self.group_scroll.inner.winfo_children():
            widget.destroy()
        visible = self._visible_groups()
        if not visible:
            ttk.Label(self.group_scroll.inner, text="No groups match the current search.").pack(anchor="w", padx=8, pady=8)
        else:
            for group in visible:
                group_id = str(group.get("id"))
                var = self._ensure_group_var(group_id)
                row = ttk.Frame(self.group_scroll.inner, padding=(6, 3))
                row.pack(fill="x")
                ttk.Checkbutton(row, text=str(group.get("displayName") or "(unnamed group)"), variable=var, command=self._update_selection_count).pack(anchor="w")
                description = str(group.get("description") or "").strip()
                if description:
                    ttk.Label(row, text=description, foreground="#666666", wraplength=720).pack(anchor="w", padx=(24, 0))
        self._update_selection_count()

    def _update_selection_count(self) -> None:
        self.selection_count_var.set(f"{sum(1 for var in self.group_vars.values() if var.get())} selected")

    def _select_all_visible(self) -> None:
        for group in self._visible_groups():
            self._ensure_group_var(str(group.get("id"))).set(True)
        self._update_selection_count()

    def _clear_visible(self) -> None:
        for group in self._visible_groups():
            self._ensure_group_var(str(group.get("id"))).set(False)
        self._update_selection_count()

    def _go_to_mapping(self) -> None:
        selected = {group_id for group_id, var in self.group_vars.items() if var.get()}
        if not selected:
            messagebox.showwarning("No groups selected", "Select at least one Entra security group before continuing.")
            return
        self.selected_group_ids = selected
        self._show_mapping_page()

    def _show_mapping_page(self) -> None:
        self._clear()
        ttk.Label(self.container, text="2. Map Groups to Zendesk Organizations", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self.container, text="Choose the Zendesk organization for each selected Entra group. Immutable IDs will be saved; names are kept for readability.", wraplength=800).pack(anchor="w", pady=(4, 12))
        header = ttk.Frame(self.container)
        header.pack(fill="x", padx=(8, 24), pady=(0, 4))
        header.columnconfigure(0, weight=1, uniform="mapping")
        header.columnconfigure(1, weight=0)
        header.columnconfigure(2, weight=1, uniform="mapping")
        ttk.Label(header, text="Entra security group", font=("Segoe UI", 10, "bold"), anchor="e").grid(row=0, column=0, sticky="e", padx=(0, 12))
        ttk.Separator(header, orient="vertical").grid(row=0, column=1, sticky="ns")
        ttk.Label(header, text="Zendesk organization", font=("Segoe UI", 10, "bold"), anchor="w").grid(row=0, column=2, sticky="w", padx=(12, 0))
        scroll = ScrollableFrame(self.container)
        scroll.pack(fill="both", expand=True, pady=(0, 10))
        org_display_to_org = {self._org_display(org): org for org in self.organizations}
        org_values = list(org_display_to_org.keys())
        self.org_display_to_org = org_display_to_org
        selected_groups = [group for group in self.groups if str(group.get("id")) in self.selected_group_ids]
        selected_groups.sort(key=lambda group: str(group.get("displayName") or "").lower())
        for group in selected_groups:
            group_id = str(group.get("id"))
            row = ttk.Frame(scroll.inner, padding=(6, 5))
            row.pack(fill="x")
            row.columnconfigure(0, weight=1, uniform="mapping")
            row.columnconfigure(1, weight=0)
            row.columnconfigure(2, weight=1, uniform="mapping")
            ttk.Label(row, text=str(group.get("displayName") or "(unnamed group)"), anchor="e").grid(row=0, column=0, sticky="e", padx=(0, 12))
            ttk.Separator(row, orient="vertical").grid(row=0, column=1, sticky="ns")
            var = self.mapping_vars.get(group_id)
            if var is None:
                var = tk.StringVar()
                self.mapping_vars[group_id] = var
            if not var.get():
                existing = self.existing_mappings.get(group_id, {})
                existing_org_id = str((existing.get("zendesk_organization") or {}).get("id") or "")
                for display, org in org_display_to_org.items():
                    if str(org.get("id")) == existing_org_id:
                        var.set(display)
                        break
            ttk.Combobox(row, textvariable=var, values=org_values, state="readonly", width=38).grid(row=0, column=2, sticky="w", padx=(12, 0))
        buttons = ttk.Frame(self.container)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="< Back", command=self._show_group_page).pack(side="left")
        ttk.Button(buttons, text="Save Configuration", command=self._save).pack(side="right")

    @staticmethod
    def _org_display(org: dict) -> str:
        return f"{org.get('name') or '(unnamed organization)'}  [ID: {org.get('id')}]"

    def _save(self) -> None:
        selected_groups = [group for group in self.groups if str(group.get("id")) in self.selected_group_ids]
        missing: list[str] = []
        mappings: list[dict] = []
        for group in sorted(selected_groups, key=lambda item: str(item.get("displayName") or "").lower()):
            group_id = str(group.get("id"))
            display = self.mapping_vars.get(group_id, tk.StringVar()).get().strip()
            org = self.org_display_to_org.get(display)
            if not org:
                missing.append(str(group.get("displayName") or group_id))
                continue
            mappings.append({
                "entra_group": {"id": group_id, "name": str(group.get("displayName") or "")},
                "zendesk_organization": {"id": int(org["id"]), "name": str(org.get("name") or "")},
            })
        if missing:
            preview = "\n".join(f"- {name}" for name in missing[:12])
            if len(missing) > 12:
                preview += f"\n- ...and {len(missing) - 12} more"
            messagebox.showwarning("Mappings incomplete", "Every selected Entra group must have a Zendesk organization.\n\n" + preview)
            return

        config = build_config(
            tenant_id=self.graph_config["tenant_id"],
            client_id=self.graph_config["client_id"],
            zendesk_subdomain=self.zendesk_config["subdomain"],
            mappings=mappings,
        )
        existing_zendesk = self.existing_config.get("zendesk") if isinstance(self.existing_config, dict) else None
        if isinstance(existing_zendesk, dict) and isinstance(existing_zendesk.get("user_fields"), dict):
            config["zendesk"]["user_fields"] = dict(existing_zendesk["user_fields"])

        try:
            path = save_config(config)
        except ConfigError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.existing_config = config
        self.existing_mappings = existing_mapping_by_group_id(config)
        self.saved_config_path = path
        messagebox.showinfo("Configuration saved", f"Configuration saved successfully to:\n{path}\n\nNo secrets were written to config.yaml.")
        self.destroy()


def main() -> int:
    try:
        app = ConfigureApp()
        app.mainloop()
        if app.saved_config_path:
            print("Configuration wizard completed successfully.")
            print(f"Saved configuration: {app.saved_config_path}")
        else:
            print("Configuration wizard closed without saving changes.")
        return 0
    except tk.TclError as exc:
        print(f"ERROR: Unable to start Tkinter configuration GUI: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
