#!/usr/bin/env python3
"""GUI for reviewing risky initial email-based Zendesk user adoptions."""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.bootstrap_review import (  # noqa: E402
    BootstrapReviewError,
    DEFAULT_DECISIONS_PATH,
    load_review_candidates,
    load_review_decisions,
    save_review_decisions,
)

LEAVE_UNRESOLVED = "Leave unresolved"
APPROVE_HR_NAME = "Use this Zendesk user and update it to the Entra/HR name"
MANUAL_REVIEW = "Do not approve automatically; leave for manual cleanup"


class BootstrapReviewApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Entra -> Zendesk Initial Match Review")
        self.geometry("920x700")
        self.minsize(780, 580)

        try:
            self.reviews = load_review_candidates()
            self.decisions = load_review_decisions()
        except BootstrapReviewError as exc:
            messagebox.showerror("Unable to load review data", str(exc))
            self.after(0, self.destroy)
            return

        self.saved = False
        self.index = 0
        self.decision_var = tk.StringVar()
        self.container = ttk.Frame(self, padding=16)
        self.container.pack(fill="both", expand=True)

        if not self.reviews:
            self._show_no_reviews()
        else:
            self._show_review()

    def _clear(self) -> None:
        for widget in self.container.winfo_children():
            widget.destroy()

    def _show_no_reviews(self) -> None:
        self._clear()
        ttk.Label(
            self.container,
            text="No initial name-mismatch reviews are pending",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            self.container,
            text="The latest dry-run snapshot did not contain email-based ADOPT/RELINK rows with a different Zendesk name.",
            wraplength=840,
        ).pack(anchor="w")
        ttk.Button(self.container, text="Close", command=self.destroy).pack(anchor="e", pady=(20, 0))

    def _show_review(self) -> None:
        self._clear()
        item = self.reviews[self.index]
        review_type = str(item.get("review_type") or "unknown")

        ttk.Label(
            self.container,
            text="Initial Identity Match Review",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            self.container,
            text=f"Review {self.index + 1} of {len(self.reviews)} | Type: {review_type}",
            foreground="#555555",
        ).pack(anchor="w", pady=(2, 10))

        warning = ttk.LabelFrame(self.container, text="Why this needs review", padding=10)
        warning.pack(fill="x", pady=(0, 12))
        ttk.Label(
            warning,
            text=(
                "This initial setup matched an existing Zendesk user by email, but the Zendesk name differs from the "
                "name provided by Entra/HR. Different names can be legitimate (nickname, preferred name, old spelling, "
                "or inconsistent historical entry), but reused email addresses can also point to a prior employee. "
                "Approving the match keeps the existing Zendesk ticket history and standardizes the Zendesk name to "
                "the Entra/HR name."
            ),
            wraplength=840,
        ).pack(anchor="w")

        entra = ttk.LabelFrame(self.container, text="Entra / HR identity", padding=10)
        entra.pack(fill="x", pady=(0, 10))
        self._detail_row(entra, "Name", item.get("name"))
        self._detail_row(entra, "Email", item.get("email"))
        self._detail_row(entra, "Entra object ID", item.get("entra_id"))
        self._detail_row(entra, "Mapped group", item.get("group_name"))
        self._detail_row(entra, "Desired organization", item.get("zendesk_org_name"))

        zendesk = ttk.LabelFrame(self.container, text="Existing Zendesk user", padding=10)
        zendesk.pack(fill="x", pady=(0, 10))
        self._detail_row(zendesk, "Zendesk user ID", item.get("zendesk_id"))
        self._detail_row(zendesk, "Current name", item.get("zendesk_name"))
        self._detail_row(zendesk, "Email", item.get("zendesk_email"))
        self._detail_row(zendesk, "Current external ID", item.get("zendesk_external_id") or "(none)")
        self._detail_row(zendesk, "Planned action", item.get("action"))

        decision_frame = ttk.LabelFrame(self.container, text="Decision", padding=10)
        decision_frame.pack(fill="x", pady=(0, 10))
        choices = [LEAVE_UNRESOLVED, APPROVE_HR_NAME, MANUAL_REVIEW]
        current = self.decisions.get(str(item.get("entra_id") or ""), {})
        current_decision = str(current.get("decision") or "")
        if current_decision == "approve_hr_name":
            selected = APPROVE_HR_NAME
        elif current_decision == "manual_review":
            selected = MANUAL_REVIEW
        else:
            selected = LEAVE_UNRESOLVED
        self.decision_var.set(selected)
        ttk.Combobox(
            decision_frame,
            textvariable=self.decision_var,
            values=choices,
            state="readonly",
        ).pack(fill="x")

        ttk.Label(
            decision_frame,
            text=(
                "Zendesk email identities are unique. If this is actually a different person, creating another user "
                "with the same email requires manual cleanup of the old Zendesk identity first."
            ),
            wraplength=820,
            foreground="#555555",
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(self.container)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="< Previous", command=self._previous).pack(side="left")
        ttk.Button(buttons, text="Next >", command=self._next).pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="Next Type >>", command=self._next_type).pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons,
            text="Apply This Decision to All Same-Type Reviews",
            command=self._apply_to_same_type,
        ).pack(side="left", padx=(16, 0))
        ttk.Button(buttons, text="Save Decisions", command=self._save_and_close).pack(side="right")

    @staticmethod
    def _detail_row(parent: tk.Misc, label: str, value: object) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=f"{label}:", width=25, anchor="e").pack(side="left", padx=(0, 8))
        ttk.Label(row, text=str(value or "-"), wraplength=650, justify="left").pack(
            side="left", fill="x", expand=True
        )

    def _store_current(self) -> None:
        if not self.reviews:
            return
        entra_id = str(self.reviews[self.index].get("entra_id") or "")
        selected = self.decision_var.get()
        if selected == APPROVE_HR_NAME:
            self.decisions[entra_id] = {"decision": "approve_hr_name"}
        elif selected == MANUAL_REVIEW:
            self.decisions[entra_id] = {"decision": "manual_review"}
        else:
            self.decisions.pop(entra_id, None)

    def _previous(self) -> None:
        self._store_current()
        if self.index > 0:
            self.index -= 1
        self._show_review()

    def _next(self) -> None:
        self._store_current()
        if self.index < len(self.reviews) - 1:
            self.index += 1
        self._show_review()

    def _find_next_type_index(self) -> int | None:
        if not self.reviews:
            return None
        current_type = str(self.reviews[self.index].get("review_type") or "")

        # Prefer the next different review type later in the list.
        for candidate_index in range(self.index + 1, len(self.reviews)):
            candidate_type = str(self.reviews[candidate_index].get("review_type") or "")
            if candidate_type != current_type:
                return candidate_index

        # If needed, wrap around so the button remains useful from the end.
        for candidate_index in range(0, self.index):
            candidate_type = str(self.reviews[candidate_index].get("review_type") or "")
            if candidate_type != current_type:
                return candidate_index

        return None

    def _next_type(self) -> None:
        self._store_current()
        next_index = self._find_next_type_index()
        if next_index is None:
            messagebox.showinfo(
                "No other review type",
                "There are no reviews of a different type in the current review set.",
            )
            return
        self.index = next_index
        self._show_review()

    def _apply_to_same_type(self) -> None:
        self._store_current()
        current = self.reviews[self.index]
        review_type = str(current.get("review_type") or "")
        entra_id = str(current.get("entra_id") or "")
        decision = self.decisions.get(entra_id, {})
        if not decision:
            messagebox.showinfo("No bulk decision", "Choose a concrete decision before applying it to other reviews.")
            return
        matching = [item for item in self.reviews if str(item.get("review_type") or "") == review_type]
        if not messagebox.askyesno(
            "Apply decision to matching reviews",
            f"Apply this decision to all {len(matching)} review(s) of type '{review_type}'?",
        ):
            return
        for item in matching:
            self.decisions[str(item.get("entra_id") or "")] = dict(decision)

        next_index = self._find_next_type_index()
        if next_index is None:
            messagebox.showinfo(
                "Bulk decision applied",
                f"Updated {len(matching)} saved decision(s). There are no other review types in this set.",
            )
            self._show_review()
            return

        messagebox.showinfo(
            "Bulk decision applied",
            f"Updated {len(matching)} saved decision(s). Moving to the next review type.",
        )
        self.index = next_index
        self._show_review()

    def _save_and_close(self) -> None:
        self._store_current()
        try:
            path = save_review_decisions(self.decisions)
        except BootstrapReviewError as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.saved = True
        messagebox.showinfo(
            "Initial match decisions saved",
            f"Saved {len(self.decisions)} decision(s) to:\n{path}\n\nRun sync.py again to rebuild the review status.",
        )
        self.destroy()


def main() -> int:
    try:
        app = BootstrapReviewApp()
        app.mainloop()
        if getattr(app, "saved", False):
            print("Initial match review completed successfully.")
            print(f"Saved decisions: {DEFAULT_DECISIONS_PATH}")
        else:
            print("Initial match review closed without saving changes.")
        return 0
    except tk.TclError as exc:
        print(f"ERROR: Unable to start Tkinter initial match review GUI: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
