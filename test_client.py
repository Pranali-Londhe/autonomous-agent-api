"""
test_client.py

Exercises the two required test inputs against a running instance of the API
(`uvicorn main:app --reload`). Prints the agent's task list, assumptions, and
downloads the generated .docx for each case.

Usage:
    python test_client.py
"""
import json
import os
import requests

BASE_URL = os.environ.get("AGENT_BASE_URL", "http://127.0.0.1:8000")

TEST_CASES = {
    "standard_business_request": (
        "Create meeting minutes for our weekly project sync between the "
        "engineering team and the client, covering sprint progress, blockers, "
        "and next week's action items."
    ),
    "complex_ambiguous_request": (
        "We need something to send to a potential client about our new AI "
        "automation service, and also something internal for the team to "
        "track how we'll actually deliver it, but I'm not sure about pricing "
        "or the timeline yet — just make reasonable calls and get it done."
    ),
}


def run_case(name: str, request_text: str):
    print("=" * 80)
    print(f"TEST CASE: {name}")
    print(f"REQUEST: {request_text}")
    print("-" * 80)

    resp = requests.post(f"{BASE_URL}/agent", json={"request": request_text}, timeout=60)
    resp.raise_for_status()
    data = resp.json()

    print(f"LLM MODE USED: {data['llm_mode']}")
    print(f"DOCUMENT TYPE: {data['plan']['document_type']}")
    print(f"TITLE: {data['plan']['title']}")
    print("\nAGENT-GENERATED TASK LIST:")
    for t in data["tasks"]:
        print(f"  [{t['status']:>28}] step {t['step_number']}: {t['name']}")

    print("\nASSUMPTIONS MADE BY THE AGENT:")
    if data["assumptions"]:
        for a in data["assumptions"]:
            print(f"  - {a}")
    else:
        print("  (none needed)")

    print(f"\nSUMMARY: {data['summary']}")

    # download the generated docx
    dl = requests.get(f"{BASE_URL}{data['download_url']}", timeout=30)
    dl.raise_for_status()
    out_path = f"generated_{name}.docx"
    with open(out_path, "wb") as f:
        f.write(dl.content)
    print(f"\nSaved document to: {out_path}")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    for name, req in TEST_CASES.items():
        run_case(name, req)
