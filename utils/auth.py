import streamlit as st
import hashlib

def check_password():
    """Returns `True` if the user had a correct password."""

    def password_entered():
        """Checks whether a password entered by the user is correct."""
        if st.session_state["username"] in st.secrets["users"] and hashlib.sha256(
            st.session_state["password"].encode()
        ).hexdigest() == st.secrets["users"].get(st.session_state["username"]):
            st.session_state["password_correct"] = True
            del st.session_state["password"]  # don't store password
        else:
            st.session_state["password_correct"] = False

    if st.session_state.get("password_correct", False):
        return True

    # Show input fields
    st.text_input("Username", key="username")
    st.text_input("Password", type="password", key="password")

    # Add a login button
    if st.button("Login"):
        password_entered()
        if not st.session_state.get("password_correct", False):
            st.error("😕 User not known or password incorrect")

    return False
