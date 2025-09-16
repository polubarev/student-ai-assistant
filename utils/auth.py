import streamlit as st
import hashlib

def check_password():
    def password_entered():
        if (
            st.session_state["username"] in st.secrets["users"]
            and hashlib.sha256(st.session_state["password"].encode()).hexdigest()
            == st.secrets["users"][st.session_state["username"]]
        ):
            st.session_state["password_correct"] = True
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", key="password", on_change=password_entered)
        return False
    elif not st.session_state["password_correct"]:
        st.error("😕 User not known or password incorrect")
        return False
    else:
        return True
