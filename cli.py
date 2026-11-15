import sys
import os
import json
from typing import Dict, Any, List

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from database import get_applications, get_application, create_saved_answer, update_application

def format_banner(text: str):
    print("\n" + "=" * 65)
    print(f"  {text}")
    print("=" * 65)

def list_applications_cli():
    apps = get_applications()
    if not apps:
        print("\nNo applications found in local database.")
        return

    format_banner("ApplyMate Job Applications (Local SQLite)")
    for app in apps:
        app_id = app["id"][:8]
        status = app["status"].upper()
        comp = app.get("company_name") or "Direct"
        stage = app.get("stage_message", "")
        questions = app.get("unanswered_questions") or []
        if not questions and app.get("unanswered_question"):
            questions = [app["unanswered_question"]]

        status_tag = f"[{status}]"
        if status == "NEEDS_INPUT":
            status_tag = f"[{status} - {len(questions)} QUESTIONS WAITING]"

        print(f"ID: {app_id} | {comp:<18} | {status_tag:<28} | {stage[:40]}")
    print()

def answer_application_cli(target_id: str = None):
    apps = get_applications()
    needs_input_apps = [
        a for a in apps if a["status"] == "needs_input" or (a.get("unanswered_questions") and len(a.get("unanswered_questions")) > 0)
    ]

    if not needs_input_apps:
        print("\n[OK] No applications are currently waiting for answers!")
        return

    selected_app = None
    if target_id:
        for a in needs_input_apps:
            if a["id"].startswith(target_id):
                selected_app = a
                break
        if not selected_app:
            print(f"Could not find waiting application matching ID '{target_id}'.")
            return
    else:
        selected_app = needs_input_apps[0]

    app_id = selected_app["id"]
    company_name = selected_app.get("company_name", "Target Company")
    questions = selected_app.get("unanswered_questions") or []
    if not questions and selected_app.get("unanswered_question"):
        questions = [selected_app["unanswered_question"]]

    if not questions:
        print(f"No unanswered questions found on application for {company_name}.")
        return

    format_banner(f"Batch Q&A for {company_name} ({len(questions)} Questions)")
    print("Answer each question below. Hit Enter to accept the AI suggestion or default scope.\n")

    batch_answers = []

    for idx, q in enumerate(questions, 1):
        lbl = q.get("label", "")
        q_type = q.get("type", "text")
        opts = q.get("options", [])
        ai_sug = q.get("ai_suggestion", "")
        def_scope = q.get("scope", "global")
        def_scope_str = "this company" if def_scope in ["company", "this company"] else "global"

        print(f"[{idx}/{len(questions)}] Question: {lbl}")
        print(f"      Type: {q_type}")
        if opts:
            print("      Options:")
            for o_idx, opt in enumerate(opts, 1):
                print(f"        [{o_idx}] {opt}")
        if ai_sug:
            print(f"      AI Suggestion: '{ai_sug}'")

        # 1. Ask for Answer (handles empty, indices, or free text gracefully)
        prompt_text = f"      Your Answer"
        if ai_sug:
            prompt_text += f" [Default: '{ai_sug}']"
        elif opts:
            prompt_text += f" [1-{len(opts)} or option text]"
        prompt_text += ": "

        try:
            raw_ans = input(prompt_text).strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nOperation cancelled by user. Exiting without saving changes.")
            return

        final_ans = ""
        if not raw_ans:
            final_ans = ai_sug or (str(opts[0]) if opts else "")
        elif opts and raw_ans.isdigit():
            chosen_idx = int(raw_ans) - 1
            if 0 <= chosen_idx < len(opts):
                final_ans = str(opts[chosen_idx])
            else:
                print(f"      [!] Index out of range; using raw text: '{raw_ans}'")
                final_ans = raw_ans
        else:
            final_ans = raw_ans

        # 2. Ask for Scope: Every site vs Just this company
        print(f"      Apply this answer to:")
        print(f"        [1] Every site (Global)")
        print(f"        [2] Just this company ({company_name})")
        try:
            scope_choice = input(f"      Choice (1 or 2) [Default: {'2' if def_scope_str == 'this company' else '1'}]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n\nOperation cancelled by user. Exiting without saving changes.")
            return

        if scope_choice == "2":
            final_scope = "this company"
        elif scope_choice == "1":
            final_scope = "global"
        else:
            final_scope = def_scope_str

        # 3. Ask for Save to Memory
        try:
            save_choice = input("      Save to Answer Memory? [Y/n, Default: Y]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n\nOperation cancelled by user. Exiting without saving changes.")
            return

        save_for_future = False if save_choice == "n" else True

        # Save to SQLite saved_answers immediately if requested
        if save_for_future and final_ans:
            try:
                create_saved_answer(
                    pattern=lbl,
                    answer=final_ans,
                    field_type=q_type,
                    category=q.get("category", "general"),
                    scope=final_scope,
                    company_name=company_name if final_scope == "this company" else "",
                    options_seen=opts,
                    source="user"
                )
                print(f"      -> Saved to Answer Memory ({final_scope} scope).")
            except Exception as e:
                print(f"      -> Warning saving to memory: {e}")

        batch_answers.append({
            "id": q.get("id"),
            "field_id": q.get("field_id", ""),
            "label": lbl,
            "type": q_type,
            "options": opts,
            "answer": final_ans,
            "scope": final_scope,
            "save_for_future": save_for_future,
            "category": q.get("category", "general")
        })
        print("-" * 65)

    # Update application with batch_answers to wake up worker
    update_application(app_id, {
        "status": "processing",
        "batch_answers": batch_answers,
        "unanswered_questions": [],
        "unanswered_question": None,
        "stage_message": f"Received {len(batch_answers)} answers via CLI. Resuming autofill..."
    })

    print(f"\n[SUCCESS] Submitted {len(batch_answers)} answers for {company_name}! The worker will resume autofill.")

def main():
    if len(sys.argv) < 2:
        print("ApplyMate CLI Helper")
        print("Usage:")
        print("  python backend/cli.py list           - List all applications")
        print("  python backend/cli.py answer [ID]    - Interactively answer waiting questions")
        return

    cmd = sys.argv[1].lower()
    if cmd in ("list", "ls"):
        list_applications_cli()
    elif cmd in ("answer", "ans", "reply"):
        target_id = sys.argv[2] if len(sys.argv) > 2 else None
        answer_application_cli(target_id)
    else:
        print(f"Unknown command '{cmd}'. Use 'list' or 'answer'.")

if __name__ == "__main__":
    main()
