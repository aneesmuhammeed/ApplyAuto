#!/usr/bin/env python3
"""
CLI Tool for Reviewing, Editing, and Deleting ApplyMate Saved Answers Memory.
Usage:
    python backend/manage_memory.py list
    python backend/manage_memory.py list --scope global
    python backend/manage_memory.py list --search "relocate"
    python backend/manage_memory.py edit <id> --answer "No" --scope "this company" --company "Acme Corp"
    python backend/manage_memory.py delete <id>
    python backend/manage_memory.py add --pattern "Willing to relocate?" --answer "No" --scope "this company" --company "Google"
"""

import sys
import os
import argparse
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import (
    get_saved_answers, create_saved_answer, update_saved_answer, delete_saved_answer
)

def format_row(cols, widths):
    formatted = []
    for c, w in zip(cols, widths):
        s = str(c or "")
        if len(s) > w:
            s = s[:w-3] + "..."
        formatted.append(s.ljust(w))
    return " | ".join(formatted)

def cmd_list(args):
    answers = get_saved_answers()
    if args.scope:
        s_low = args.scope.lower()
        answers = [a for a in answers if s_low in (a.get("scope") or "").lower()]
    if args.search:
        q_low = args.search.lower()
        answers = [
            a for a in answers
            if q_low in (a.get("question_pattern") or "").lower()
            or q_low in (a.get("answer") or "").lower()
            or q_low in (a.get("company_name") or "").lower()
        ]

    if not answers:
        print("\nNo saved answers found matching criteria.")
        return

    print(f"\nSaved Answers Memory ({len(answers)} record(s)):")
    headers = ["ID", "Question Pattern", "Field Type", "Answer", "Scope", "Company", "Uses", "Last Used"]
    widths = [8, 30, 10, 20, 12, 15, 6, 19]

    divider = "-+-".join(["-" * w for w in widths])
    print(format_row(headers, widths))
    print(divider)

    for item in answers:
        item_id = (item.get("id") or "")[:8]
        pat = item.get("question_pattern") or item.get("normalized_text") or ""
        ftype = item.get("field_type") or "text"
        ans = item.get("answer") or ""
        scope = item.get("scope") or "global"
        comp = item.get("company_name") or "-"
        uses = item.get("usage_count") or 0
        last_u = item.get("last_used") or item.get("created_at") or "-"

        row = [item_id, pat, ftype, ans, scope, comp, uses, last_u]
        print(format_row(row, widths))
    print(f"{len(answers)} total answer(s) in local SQLite memory.\n")

def cmd_edit(args):
    ans_id = args.id.strip()
    answers = get_saved_answers()
    matched = [a for a in answers if a.get("id") == ans_id or (a.get("id") or "").startswith(ans_id)]
    if not matched:
        print(f"Error: No saved answer found matching ID '{ans_id}'.")
        sys.exit(1)

    full_id = matched[0]["id"]
    updates = {}
    if args.pattern is not None:
        updates["question_pattern"] = args.pattern
    if args.answer is not None:
        updates["answer"] = args.answer
    if args.field_type is not None:
        updates["field_type"] = args.field_type
    if args.category is not None:
        updates["category"] = args.category
    if args.scope is not None:
        updates["scope"] = "this company" if args.scope in ["company", "this company"] else "global"
    if args.company is not None:
        updates["company_name"] = args.company

    if not updates:
        print("No changes specified. Use --answer, --pattern, --scope, --company, etc.")
        return

    update_saved_answer(full_id, updates)
    print(f"Successfully updated saved answer '{full_id[:8]}' with: {updates}")

def cmd_delete(args):
    ans_id = args.id.strip()
    answers = get_saved_answers()
    matched = [a for a in answers if a.get("id") == ans_id or (a.get("id") or "").startswith(ans_id)]
    if not matched:
        print(f"Error: No saved answer found matching ID '{ans_id}'.")
        sys.exit(1)

    full_id = matched[0]["id"]
    pat = matched[0].get("question_pattern")
    ans = matched[0].get("answer")

    if not getattr(args, "yes", False):
        confirm = input(f"Are you sure you want to delete answer for '{pat}' -> '{ans}'? [y/N]: ").strip().lower()
        if confirm not in ["y", "yes"]:
            print("Deletion cancelled.")
            return

    delete_saved_answer(full_id)
    print(f"Deleted saved answer '{full_id[:8]}' ('{pat}') from memory.")

def cmd_add(args):
    if not args.pattern or not args.answer:
        print("Error: --pattern and --answer are required.")
        sys.exit(1)

    scope = "this company" if args.scope in ["company", "this company"] else "global"
    ans_id = create_saved_answer(
        pattern=args.pattern,
        answer=args.answer,
        field_type=args.field_type or "text",
        category=args.category or "general",
        scope=scope,
        company_name=args.company or "",
        source="user"
    )
    print(f"Added saved answer '{ans_id[:8]}' (scope: {scope}): '{args.pattern}' -> '{args.answer}'")

def main():
    parser = argparse.ArgumentParser(description="Manage ApplyMate Saved Answers Memory")
    subparsers = parser.add_subparsers(dest="command", help="Subcommand to run")

    # list
    p_list = subparsers.add_parser("list", help="List all saved answers with scope, usage count, and last used time")
    p_list.add_argument("--scope", choices=["global", "company", "this company"], help="Filter by scope")
    p_list.add_argument("--search", help="Search keyword across pattern, answer, company")

    # edit
    p_edit = subparsers.add_parser("edit", help="Edit an existing saved answer")
    p_edit.add_argument("id", help="Saved answer ID (or first 8 chars)")
    p_edit.add_argument("--answer", help="New answer text")
    p_edit.add_argument("--pattern", help="New question pattern")
    p_edit.add_argument("--field-type", help="New field type (text, select, radio, etc.)")
    p_edit.add_argument("--category", help="Category (general, legal, compensation, etc.)")
    p_edit.add_argument("--scope", choices=["global", "company", "this company"], help="Scope (global or this company)")
    p_edit.add_argument("--company", help="Company name (for company-specific scope)")

    # delete
    p_del = subparsers.add_parser("delete", help="Delete a saved answer by ID")
    p_del.add_argument("id", help="Saved answer ID (or first 8 chars)")
    p_del.add_argument("-y", "--yes", action="store_true", help="Confirm deletion without prompting")

    # add
    p_add = subparsers.add_parser("add", help="Add a new saved answer")
    p_add.add_argument("--pattern", required=True, help="Question pattern")
    p_add.add_argument("--answer", required=True, help="Approved answer")
    p_add.add_argument("--field-type", default="text", help="Field type")
    p_add.add_argument("--category", default="general", help="Category")
    p_add.add_argument("--scope", choices=["global", "company", "this company"], default="global", help="Scope")
    p_add.add_argument("--company", default="", help="Company name (if company scope)")

    args = parser.parse_args()
    if not args.command or args.command == "list":
        cmd_list(args if args.command else argparse.Namespace(scope=None, search=None))
    elif args.command == "edit":
        cmd_edit(args)
    elif args.command == "delete":
        cmd_delete(args)
    elif args.command == "add":
        cmd_add(args)

if __name__ == "__main__":
    main()
