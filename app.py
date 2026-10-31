"""Simple web chat UI.   streamlit run app.py"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from emporio_agent.agent import EmporioAgent  # noqa: E402

st.set_page_config(page_title="Empório da Música — Atendimento", page_icon="🎸")


@st.cache_resource
def get_agent() -> EmporioAgent:
    return EmporioAgent()


agent = get_agent()

# The session id lives in the URL (?session=...) so a page reload keeps the conversation.
if "session" not in st.query_params:
    st.query_params["session"] = f"web-{uuid.uuid4().hex[:8]}"
session = st.query_params["session"]

with st.sidebar:
    st.markdown("### 🎸 Empório da Música")
    st.caption("Protótipo de atendimento com IA — Lia, assistente virtual.")
    show_tools = st.toggle("Mostrar ferramentas usadas", value=True)
    st.caption(f"Conversa: `{session}`")
    if st.button("Nova conversa"):
        st.query_params["session"] = f"web-{uuid.uuid4().hex[:8]}"
        st.session_state.pop("tool_log", None)
        st.rerun()

st.title("Fale com a Lia")
tool_log: dict[int, list] = st.session_state.setdefault("tool_log", {})

history = agent.history_for_display(session)
if not history:
    with st.chat_message("assistant"):
        st.write("Oi! Eu sou a Lia, da Empório da Música 🎶 Posso ajudar com instrumentos, "
                 "preços, pedidos ou informações da loja.")
for i, (role, text) in enumerate(history):
    with st.chat_message(role):
        st.markdown(text)
        if show_tools and role == "assistant" and i in tool_log:
            for call in tool_log[i]:
                with st.expander(f"🔧 {call.name}"):
                    st.code(json.dumps(call.input, ensure_ascii=False, indent=2), language="json")
                    st.code(call.output[:3000], language="json")

if prompt := st.chat_input("Digite sua mensagem"):
    with st.chat_message("user"):
        st.markdown(prompt)
    with st.chat_message("assistant"):
        with st.spinner("Consultando..."):
            reply = agent.chat(session, prompt)
        st.markdown(reply.text)
    if reply.tool_calls and not reply.error:
        tool_log[len(history) + 1] = reply.tool_calls
    st.rerun()
