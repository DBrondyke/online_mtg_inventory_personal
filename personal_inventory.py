import streamlit as st

if not st.user.is_logged_in:
    if st.button("Admin login"):
        st.login()
    st.stop()

st.write("Logged in:", st.user.is_logged_in)
st.write("Email:", st.user.email)

if st.button("Log out"):
    st.logout()