#!/usr/bin/env python3
"""GUI for reviewing and persisting reconciliation conflict decisions."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.conflicts import ConflictSnapshotError, load_conflicts  # noqa: E402
from lib.resolutions import (  # noqa: E402
    DEFAULT_RESOLUTIONS_PATH,
    ResolutionError,
    load_resolutions,
    save_resolutions,
)

LEAVE_UNRESOLVED = "Leave unresolved"
SKIP_USER = "Skip this Entra user"
REPLACE_EXTERNAL = "Use matched Zendesk user and replace its existing external ID"


class ConflictResolver(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Entra -> Zendesk Conflict Review")
        self.geometry("900x680")
        self.minsize(760, 560)

        try:
            self.conflicts = load_conflicts()
            self.resolutions = load_resolutions()
        except (ConflictSnapshotError, ResolutionError) as exc:
            messagebox.showerror("Unable to load conflicts", str(exc))
            self.after(0, self.destroy)
            return

        self.saved = False
        self.index = 0
        self.decision_var = tk.StringVar()
        self.container = ttk.Frame(self, padding=16)
        self.container.pack(fill="both", expand=True)

        if not self.conflicts:
            self._show_no_conflicts()
        else:
            self._show_conflict()

    def _clear(self) -> None:
        for widget in self.container.winfo_children():
            widget.destroy()

    def _show_no_conflicts(self) -> None:
        self._clear()
        ttk.Label(
            self.container,
            text="No unresolved conflicts",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            self.container,
            text="The latest reconciliation snapshot contains no conflicts that require a decision.",
            wraplength=820,
        ).pack(anchor="w")
        ttk.Button(self.container, text="Close", command=self.destroy).pack(anchor="e", pady=(20, 0))

    def _show_conflict(self) -> None:
        self._clear()
        conflict = self.conflicts[self.index]
        conflict_type = str(conflict.get("conflict_type") or "unknown")

        ttk.Label(
            self.container,
            text="Conflict Review",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            self.container,
            text=f"Conflict {self.index + 1} of {len(self.conflicts)}",
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 10))

        warning = ttk.LabelFrame(self.container, text="Important: reused email addresses", padding=10)
        warning.pack(fill="x", pady=(0, 12))
        ttk.Label(
            warning,
            text=(
                "During initial adoption, an email match may point to a Zendesk user that belonged to "
                "someone else if your organization reuses email addresses. Choosing to adopt/relink that "
                "Zendesk user keeps its existing ticket history. After a user is linked with the immutable "
                "Entra object ID, future syncs match that external ID first. A different Entra user later "
                "reusing the same email will be raised as a conflict instead of silently taking over the "
                "linked Zendesk user."
            ),
            wraplength=820,
        ).pack(anchor="w")

        details = ttk.LabelFrame(self.container, text="Entra user", padding=10)
        details.pack(fill="x", pady=(0, 10))
        self._detail_row(details, "Name", conflict.get("name"))
        self._detail_row(details, "Email", conflict.get("email"))
        self._detail_row(details, "Entra object ID", conflict.get("entra_id"))
        if conflict.get("group_name"):
            self._detail_row(details, "Mapped group", conflict.get("group_name"))
        if conflict.get("zendesk_org_name"):
            self._detail_row(
                details,
                "Desired Zendesk organization",
                f"{conflict.get('zendesk_org_name')} [ID: {conflict.get('zendesk_org_id')}]",
            )

        conflict_box = ttk.LabelFrame(self.container, text="Conflict", padding=10)
        conflict_box.pack(fill="both", expand=True, pady=(0, 10))
        self._detail_row(conflict_box, "Type", conflict_type)
        self._detail_row(conflict_box, "Reason", conflict.get("reason"))
        self._render_candidates(conflict_box, conflict)

        decision_frame = ttk.LabelFrame(self.container, text="Decision", padding=10)
        decision_frame.pack(fill="x", pady=(0, 10))
        choices = self._decision_choices(conflict)
        self.decision_map = {label: value for label, value in choices}
        self.reverse_decision_map = {self._resolution_key(value): label for label, value in choices}
        current = self.resolutions.get(str(conflict.get("entra_id") or ""), {})
        selected = self.reverse_decision_map.get(self._resolution_key(current), LEAVE_UNRESOLVED)
        self.decision_var.set(selected)
        combo = ttk.Combobox(
            decision_frame,
            textvariable=self.decision_var,
            values=[label for label, _value in choices],
            state="readonly",
        )
        combo.pack(fill="x")

        buttons = ttk.Frame(self.container)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="< Previous", command=self._previous).pack(side="left")
        ttk.Button(buttons, text="Next >", command=self._next).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons,
            text="Apply This Decision to All Same-Type Conflicts",
            command=self._apply_to_same_type,
        ).pack(side="left", padx=(16, 0))
        ttk.Button(buttons, text="Save Decisions", command=self._save_and_close).pack(side="right")

    @staticmethod
    def _detail_row(parent: tk.Misc, label: str, value: object) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=f"{label}:", width=28, anchor="e").pack(side="left", padx=(0, 8))
        ttk.Label(row, text=str(value or "-"), wraplength=620, justify="left").pack(
            side="left", fill="x", expand=True
        )

    def _render_candidates(self, parent: tk.Misc, conflict: dict) -> None:
        candidates = conflict.get("zendesk_candidates") or []
        if candidates:
            ttk.Separator(parent).pack(fill="x", pady=8)
            ttk.Label(parent, text="Zendesk candidate(s):", font=("Segoe UI", 9, "bold")).pack(anchor="w")
            for candidate in candidates:
                text = (
                    f"ID {candidate.get('id')} | {candidate.get('name') or '-'} | "
                    f"{candidate.get('email') or '-'} | external_id: "
                    f"{candidate.get('external_id') or '(none)'}"
                )
                ttk.Label(parent, text=text, wraplength=800).pack(anchor="w", padx=(12, 0), pady=2)

        groups = conflict.get("group_candidates") or []
        if groups:
            ttk.Separator(parent).pack(fill="x", pady=8)
            ttk.Label(parent, text="Mapped group choices:", font=("Segoe UI", 9, "bold")).pack(anchor="w")
            for group in groups:
                text = (
                    f"{group.get('group_name')} -> {group.get('zendesk_org_name')} "
                    f"[Org ID: {group.get('zendesk_org_id')}]"
                )
                ttk.Label(parent, text=text, wraplength=800).pack(anchor="w", padx=(12, 0), pady=2)

    def _decision_choices(self, conflict: dict) -> list[tuple[str, dict]]:
        conflict_type = str(conflict.get("conflict_type") or "")
        choices: list[tuple[str, dict]] = [
            (LEAVE_UNRESOLVED, {}),
            (SKIP_USER, {"decision": "skip"}),
        ]

        if conflict_type == "email_external_id_mismatch":
            candidates = conflict.get("zendesk_candidates") or []
            candidate_id = candidates[0].get("id") if candidates else conflict.get("zendesk_id")
            choices.insert(
                1,
                (
                    REPLACE_EXTERNAL,
                    {"decision": "replace_external_id", "zendesk_user_id": candidate_id},
                ),
            )
        elif conflict_type == "multiple_email_matches":
            for candidate in conflict.get("zendesk_candidates") or []:
                label = (
                    f"Use Zendesk user {candidate.get('id')}: "
                    f"{candidate.get('name') or '-'} <{candidate.get('email') or '-'}>"
                )
                choices.insert(
                    len(choices) - 1,
                    {"label": label, "value": {"decision": "use_zendesk_user", "zendesk_user_id": candidate.get("id")}},
                )
            choices = [
                (item["label"], item["value"]) if isinstance(item, dict) else item
                for item in choices
            ]
        elif conflict_type == "multiple_groups":
            for group in conflict.get("group_candidates") or []:
                label = f"Use {group.get('group_name')} -> {group.get('zendesk_org_name')}"
                choices.insert(
                    len(choices) - 1,
                    {"label": label, "value": {"decision": "use_group", "group_id": group.get("group_id")}},
                )
            choices = [
                (item["label"], item["value"]) if isinstance(item, dict) else item
                for item in choices
            ]

        return choices

    @staticmethod
    def _resolution_key(value: dict) -> tuple:
        if not value:
            return ()
        return (
            str(value.get("decision") or ""),
            str(value.get("zendesk_user_id") or ""),
            str(value.get("group_id") or ""),
        )

    def _store_current(self) -> None:
        if not self.conflicts:
            return
        conflict = self.conflicts[self.index]
        entra_id = str(conflict.get("entra_id") or "")
        decision = self.decision_map.get(self.decision_var.get(), {})
        if decision:
            self.resolutions[entra_id] = dict(decision)
        else:
            self.resolutions.pop(entra_id, None)

    def _previous(self) -> None:
        self._store_current()
        if self.index > 0:
            self.index -= 1
        self._show_conflict()

    def _next(self) -> None:
        self._store_current()
        if self.index < len(self.conflicts) - 1:
            self.index += 1
        self._show_conflict()

    def _apply_to_same_type(self) -> None:
        self._store_current()
        current = self.conflicts[self.index]
        conflict_type = str(current.get("conflict_type") or "")
        decision = self.resolutions.get(str(current.get("entra_id") or ""), {})
        if not decision:
            messagebox.showinfo(
                "No bulk decision",
                "Choose a concrete decision before applying it to other conflicts.",
            )
            return
        if decision.get("decision") in {"use_zendesk_user", "use_group"}:
            messagebox.showinfo(
                "Bulk decision not available",
                "This decision references a specific candidate and cannot be safely copied to other conflicts.",
            )
            return
        matching = [item for item in self.conflicts if str(item.get("conflict_type") or "") == conflict_type]
        if not messagebox.askyesno(
            "Apply decision to matching conflicts",
            f"Apply this decision to all {len(matching)} conflict(s) of type '{conflict_type}'?",
        ):
            return
        for item in matching:
            entra_id = str(item.get("entra_id") or "")
            copied = dict(decision)
            if copied.get("decision") == "replace_external_id":
                candidates = item.get("zendesk_candidates") or []
                copied["zendesk_user_id"] = candidates[0].get("id") if candidates else item.get("zendesk_id")
            self.resolutions[entra_id] = copied
        messagebox.showinfo("Bulk decision applied", f"Updated {len(matching)} saved decision(s).")
        self._show_conflict()

    def _save_and_close(self) -> None:
        self._store_current()
        try:
            path = save_resolutions(self.resolutions)
        except ResolutionError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.saved = True
        messagebox.showinfo(
            "Conflict decisions saved",
            f"Saved {len(self.resolutions)} decision(s) to:\n{path}\n\nRun sync.py again to rebuild the plan.",
        )
        self.destroy()


def main() -> int:
    try:
        app = ConflictResolver()
        app.mainloop()
        if getattr(app, "saved", False):
            print("Conflict review completed successfully.")
            print(f"Saved decisions: {DEFAULT_RESOLUTIONS_PATH}")
        else:
            print("Conflict review closed without saving changes.")
        return 0
    except tk.TclError as exc:
        print(f"ERROR: Unable to start Tkinter conflict review GUI: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
