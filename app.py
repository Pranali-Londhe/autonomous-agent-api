"""
streamlit_app.py

Streamlit UI for the Autonomous Document Agent FastAPI service.

Run the FastAPI backend first (in a separate terminal):
    uvicorn main:app --reload --port 8000

Then run this UI (in another terminal, same venv):
    streamlit run streamlit_app.py

The UI talks to the FastAPI service over HTTP, so both must be running
at the same time. Set AGENT_API_URL env var if your API runs on a
different host/port.
"""

import os
import requests
import streamlit as st

API_BASE_URL = os.environ.get("AGENT_API_URL", "http://127.0.0.1:8000")

st.set_page_config(
    page_title="Autonomous Document Agent",
    page_icon="📄",
    layout="centered",
)

st.title("📄 Autonomous Document Agent")
st.caption(
    "Describe the document you need in plain English. The agent will plan, "
    "execute, and generate a Word document for you."
)

# ---------------------------------------------------------------------------
# Sidebar: backend health / config
# ---------------------------------------------------------------------------
with st.sidebar:
    st.subheader("Backend status")
    st.text_input("API base URL", value=API_BASE_URL, key="api_url", disabled=True)

    if st.button("Check health", use_container_width=True):
        try:
            resp = requests.get(f"{API_BASE_URL}/health", timeout=10)
            resp.raise_for_status()
            st.success("API is reachable ✅")
            st.json(resp.json())
        except requests.exceptions.ConnectionError:
            st.error("Could not connect. Is `uvicorn main:app --reload --port 8000` running?")
        except Exception as e:
            st.error(f"Health check failed: {e}")

    st.divider()
    st.caption("Make sure the FastAPI service is running before submitting a request.")

# ---------------------------------------------------------------------------
# Main input form
# ---------------------------------------------------------------------------
if "history" not in st.session_state:
    st.session_state.history = []  # list of result dicts, most recent first

with st.form("agent_form", clear_on_submit=False):
    request_text = st.text_area(
        "What document do you need?",
        placeholder=(
            "e.g. Draft a formal business proposal to a client for a 3-month "
            "data analytics consulting engagement, including scope, timeline, "
            "and pricing assumptions."
        ),
        height=150,
        max_chars=4000,
    )
    submitted = st.form_submit_button("Generate document", type="primary", use_container_width=True)

if submitted:
    stripped = request_text.strip()
    if not stripped:
        st.warning("Please enter a request before submitting.")
    elif len(stripped) < 8:
        st.warning("Your request is too short for the agent to plan a meaningful document.")
    else:
        with st.spinner("Agent is planning and executing your request..."):
            try:
                resp = requests.post(
                    f"{API_BASE_URL}/agent",
                    json={"request": stripped},
                    timeout=180,
                )
                if resp.status_code == 200:
                    result = resp.json()
                    st.session_state.history.insert(0, result)
                    st.success("Document generated successfully!")
                else:
                    try:
                        detail = resp.json().get("detail", resp.text)
                    except Exception:
                        detail = resp.text
                    st.error(f"Request failed ({resp.status_code}): {detail}")
            except requests.exceptions.ConnectionError:
                st.error(
                    "Could not connect to the API. Start it with "
                    "`uvicorn main:app --reload --port 8000` and try again."
                )
            except requests.exceptions.Timeout:
                st.error("The request timed out. Try a shorter or simpler request.")
            except Exception as e:
                st.error(f"Unexpected error: {e}")

# ---------------------------------------------------------------------------
# Results display
# ---------------------------------------------------------------------------
if st.session_state.history:
    st.divider()
    st.subheader("Results")

    for i, result in enumerate(st.session_state.history):
        is_latest = i == 0
        preview = result["request"][:80] + ("..." if len(result["request"]) > 80 else "")
        with st.expander(f"📝 {preview}", expanded=is_latest):
            st.markdown(f"**LLM mode:** `{result.get('llm_mode', 'unknown')}`")

            st.markdown("**Plan**")
            st.write(result.get("plan", "—"))

            tasks = result.get("tasks") or []
            if tasks:
                st.markdown("**Tasks executed**")
                for t in tasks:
                    st.markdown(f"- {t}")

            assumptions = result.get("assumptions") or []
            if assumptions:
                st.markdown("**Assumptions made**")
                for a in assumptions:
                    st.markdown(f"- {a}")

            st.markdown("**Summary**")
            st.write(result.get("summary", "—"))

            filename = result.get("document_filename")
            if filename:
                download_url = f"{API_BASE_URL}{result.get('download_url', f'/download/{filename}')}"
                try:
                    file_resp = requests.get(download_url, timeout=30)
                    if file_resp.status_code == 200:
                        st.download_button(
                            label=f"⬇️ Download {filename}",
                            data=file_resp.content,
                            file_name=filename,
                            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            key=f"download_{i}_{filename}",
                            use_container_width=True,
                        )
                    else:
                        st.warning(f"Could not fetch file for download (status {file_resp.status_code}).")
                except Exception as e:
                    st.warning(f"Could not fetch file for download: {e}")
else:
    st.info("Submit a request above to generate your first document.")