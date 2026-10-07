import hashlib
import hmac
import time

import streamlit as st

from utils.credentials import load_users, verify_password
from utils.limits import LOGIN_ATTEMPTS, LOGIN_TOTAL
from utils.workspace import SESSION_FILES

def check_password():
    """Fail closed on legacy hashes and revoke sessions after credential changes."""
    try:
        users = load_users()
    except (OSError, ValueError):
        st.error("Вход пока не настроен. Обратитесь к администратору.")
        return False

    def record_fingerprint(username):
        return hashlib.sha256(users.get(username, "").encode()).hexdigest()

    username = st.session_state.get("authenticated_user", "")
    if st.session_state.get("password_correct"):
        still_valid = (username in users
                       and time.time() - st.session_state.get("authenticated_at", 0) < 8 * 3600
                       and hmac.compare_digest(st.session_state.get("auth_record", ""),
                                               record_fingerprint(username)))
        if still_valid:
            return True
        SESSION_FILES.clear(st.session_state)
        st.session_state.clear()

    def password_entered():
        candidate = st.session_state.get("username", "")[:128]
        password = st.session_state.pop("password", "")
        allowed = LOGIN_TOTAL.consume("all") and LOGIN_ATTEMPTS.consume(candidate)
        valid = allowed and verify_password(password, users.get(candidate))
        st.session_state["password_correct"] = bool(valid)
        st.session_state["auth_error"] = not valid
        if valid:
            st.session_state["authenticated_user"] = candidate
            st.session_state["authenticated_at"] = time.time()
            st.session_state["auth_record"] = record_fingerprint(candidate)

    with st.form("login"):
        st.text_input("Логин", key="username", max_chars=128)
        st.text_input("Пароль", type="password", key="password", max_chars=256)
        submitted = st.form_submit_button("Войти", on_click=password_entered)
    if submitted and st.session_state.get("password_correct"):
        st.rerun()
    if st.session_state.get("auth_error"):
        st.error("Неверный логин или пароль, либо превышен лимит попыток. Попробуйте позже.")
    return False
