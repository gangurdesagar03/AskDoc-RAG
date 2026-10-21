
"""
AskDoc — Streamlit document assistant
Run:
C:\MyPython\python.exe -m streamlit run app.py
"""

from __future__ import annotations

import os
import re
import json
import uuid
import hmac
import hashlib
import sqlite3
import logging
import traceback
from pathlib import Path
from datetime import datetime

import streamlit as st
import rag


# ---------------- CONFIG ----------------

APP_TITLE = "AskDoc"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "askdoc_app.db"
UPLOAD_DIR = BASE_DIR / "uploads"
LOG_DIR = BASE_DIR / "logs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

APP_LOG = LOG_DIR / "app.log"
ERROR_LOG = LOG_DIR / "errors.log"

logging.basicConfig(
    filename=str(APP_LOG),
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("askdoc_app")

error_logger = logging.getLogger("askdoc_errors")
error_handler = logging.FileHandler(ERROR_LOG, encoding="utf-8")
error_handler.setFormatter(logging.Formatter(
    "%(asctime)s - %(levelname)s - %(message)s"
))
if not error_logger.handlers:
    error_logger.addHandler(error_handler)
error_logger.setLevel(logging.ERROR)

MAX_FILE_MB = 20
ALLOWED_EXTENSIONS = {"pdf", "txt", "md", "html", "csv", "docx"}

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📄",
    layout="wide",
)


# ---------------- ERROR HISTORY ----------------

def init_error_history():
    if "error_history" not in st.session_state:
        st.session_state.error_history = []


def record_error(location, exc, question=""):
    """Keep errors in session state across Streamlit reruns."""
    init_error_history()

    error_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    details = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )

    entry = {
        "id": error_id,
        "time": timestamp,
        "location": str(location),
        "question": str(question or ""),
        "message": str(exc),
        "traceback": details,
    }

    st.session_state.error_history.insert(0, entry)

    # Avoid unlimited session growth.
    st.session_state.error_history = (
        st.session_state.error_history[:50]
    )

    error_logger.error(
        "Location: %s\nQuestion: %s\n%s",
        location,
        question,
        details,
    )
    logger.exception("Error at %s", location)


def show_error_history():
    init_error_history()
    errors = st.session_state.error_history

    st.divider()
    st.subheader("⚠️ Error History")

    if not errors:
        st.success("No errors recorded in this session.")
        return

    st.caption(
        f"{len(errors)} error(s) recorded. "
        "Errors remain here until you clear them or sign out."
    )

    if st.button(
        "🗑️ Clear error history",
        key="clear_error_history",
    ):
        st.session_state.error_history = []
        st.rerun()

    for number, entry in enumerate(errors, start=1):
        title = (
            f"Error {number} · {entry['time']} · "
            f"{entry['location']}"
        )

        with st.expander(title, expanded=(number == 1)):
            st.error(entry["message"])

            if entry["question"]:
                st.markdown("**Question:**")
                st.write(entry["question"])

            st.markdown("**Location:**")
            st.write(entry["location"])

            st.markdown("**Full traceback:**")
            st.code(entry["traceback"], language="text")


# ---------------- DATABASE ----------------

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS collections (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, name),
                FOREIGN KEY(user_id) REFERENCES users(id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS chats (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                chat_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                sources TEXT,
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                chat_id TEXT,
                rating TEXT NOT NULL,
                comment TEXT,
                created_at TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                event_type TEXT NOT NULL,
                detail TEXT,
                created_at TEXT NOT NULL
            )
        """)


def log_event(user_id, event_type, detail=""):
    try:
        with db() as con:
            con.execute("""
                INSERT INTO events
                (id, user_id, event_type, detail, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                str(uuid.uuid4()),
                user_id,
                event_type,
                str(detail)[:1000],
                datetime.now().isoformat(timespec="seconds"),
            ))
    except Exception as exc:
        logger.exception("Could not write event log: %s", exc)


# ---------------- AUTH ----------------

def hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        300_000,
    ).hex()


def register_user(username, password):
    username = username.strip().lower()

    if not re.fullmatch(r"[a-z0-9_.-]{3,30}", username):
        return False, "Username: use 3–30 letters, numbers, ., _ or -."

    if len(password) < 8:
        return False, "Password must be at least 8 characters."

    salt = os.urandom(16).hex()
    password_hash = hash_password(password, salt)
    user_id = str(uuid.uuid4())
    now = datetime.now().isoformat(timespec="seconds")

    try:
        with db() as con:
            con.execute("""
                INSERT INTO users
                (id, username, password_hash, salt, role, created_at)
                VALUES (?, ?, ?, ?, 'user', ?)
            """, (user_id, username, password_hash, salt, now))

            collection_id = str(uuid.uuid4())
            con.execute("""
                INSERT INTO collections
                (id, user_id, name, created_at)
                VALUES (?, ?, ?, ?)
            """, (collection_id, user_id, "My Documents", now))

        return True, "Account created. You can now sign in."

    except sqlite3.IntegrityError:
        return False, "That username is already taken."


def login_user(username, password):
    username = username.strip().lower()

    with db() as con:
        user = con.execute(
            "SELECT * FROM users WHERE username = ?",
            (username,),
        ).fetchone()

    if not user:
        return None

    candidate = hash_password(password, user["salt"])
    if not hmac.compare_digest(candidate, user["password_hash"]):
        return None

    return dict(user)


def require_collection_owner(user_id, collection_id):
    with db() as con:
        row = con.execute("""
            SELECT id FROM collections
            WHERE id = ? AND user_id = ?
        """, (collection_id, user_id)).fetchone()
    return row is not None


# ---------------- COLLECTIONS ----------------

def get_collections(user_id):
    with db() as con:
        return con.execute("""
            SELECT id, name, created_at
            FROM collections
            WHERE user_id = ?
            ORDER BY created_at
        """, (user_id,)).fetchall()


def create_collection(user_id, name):
    name = name.strip()
    if not name or len(name) > 60:
        return False, "Collection name must be 1–60 characters."

    try:
        with db() as con:
            con.execute("""
                INSERT INTO collections
                (id, user_id, name, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                str(uuid.uuid4()),
                user_id,
                name,
                datetime.now().isoformat(timespec="seconds"),
            ))
        return True, "Collection created."
    except sqlite3.IntegrityError:
        return False, "A collection with that name already exists."


def delete_collection(user_id, collection_id):
    if not require_collection_owner(user_id, collection_id):
        return False

    with db() as con:
        chats = con.execute("""
            SELECT id FROM chats
            WHERE user_id = ? AND collection_id = ?
        """, (user_id, collection_id)).fetchall()

        for chat in chats:
            con.execute(
                "DELETE FROM messages WHERE chat_id = ?",
                (chat["id"],),
            )
            con.execute(
                "DELETE FROM feedback WHERE chat_id = ?",
                (chat["id"],),
            )

        con.execute("""
            DELETE FROM chats
            WHERE user_id = ? AND collection_id = ?
        """, (user_id, collection_id))

        con.execute("""
            DELETE FROM collections
            WHERE id = ? AND user_id = ?
        """, (collection_id, user_id))

    try:
        rag.delete_vectorstore(
            user_id=user_id,
            collection_id=collection_id,
        )
        folder = UPLOAD_DIR / user_id / collection_id
        if folder.exists():
            import shutil
            shutil.rmtree(folder)
    except Exception as exc:
        record_error("Delete collection cleanup", exc)

    return True


# ---------------- CHAT HISTORY ----------------

def get_chats(user_id, collection_id):
    with db() as con:
        return con.execute("""
            SELECT id, title, created_at
            FROM chats
            WHERE user_id = ? AND collection_id = ?
            ORDER BY created_at DESC
        """, (user_id, collection_id)).fetchall()


def create_chat(user_id, collection_id, title="New chat"):
    chat_id = str(uuid.uuid4())
    with db() as con:
        con.execute("""
            INSERT INTO chats
            (id, user_id, collection_id, title, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (
            chat_id,
            user_id,
            collection_id,
            title[:100],
            datetime.now().isoformat(timespec="seconds"),
        ))
    return chat_id


def get_messages(chat_id):
    with db() as con:
        return con.execute("""
            SELECT role, content, sources
            FROM messages
            WHERE chat_id = ?
            ORDER BY created_at, rowid
        """, (chat_id,)).fetchall()


def save_message(chat_id, role, content, sources=None):
    with db() as con:
        con.execute("""
            INSERT INTO messages
            (id, chat_id, role, content, sources, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            str(uuid.uuid4()),
            chat_id,
            role,
            content,
            json.dumps(sources or [], ensure_ascii=False),
            datetime.now().isoformat(timespec="seconds"),
        ))


def set_chat_title(chat_id, title):
    with db() as con:
        con.execute(
            "UPDATE chats SET title = ? WHERE id = ?",
            (title[:100], chat_id),
        )


def clear_chat_history(user_id, collection_id=None):
    with db() as con:
        if collection_id is not None:
            if not require_collection_owner(user_id, collection_id):
                return False, 0

            rows = con.execute("""
                SELECT id FROM chats
                WHERE user_id = ? AND collection_id = ?
            """, (user_id, collection_id)).fetchall()
        else:
            rows = con.execute(
                "SELECT id FROM chats WHERE user_id = ?",
                (user_id,),
            ).fetchall()

        chat_ids = [row["id"] for row in rows]
        if not chat_ids:
            return True, 0

        placeholders = ",".join("?" for _ in chat_ids)
        con.execute(
            f"DELETE FROM messages WHERE chat_id IN ({placeholders})",
            chat_ids,
        )
        con.execute(
            f"DELETE FROM feedback WHERE chat_id IN ({placeholders})",
            chat_ids,
        )

        if collection_id is not None:
            con.execute("""
                DELETE FROM chats
                WHERE user_id = ? AND collection_id = ?
            """, (user_id, collection_id))
        else:
            con.execute(
                "DELETE FROM chats WHERE user_id = ?",
                (user_id,),
            )

    return True, len(chat_ids)


# ---------------- RAG INTEGRATION ----------------

def upload_document(uploaded_file, user_id, collection_id):
    suffix = Path(uploaded_file.name).suffix.lower().lstrip(".")
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("Unsupported file type.")

    data = uploaded_file.getvalue()
    if len(data) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"File exceeds {MAX_FILE_MB} MB.")

    files = [{
        "name": uploaded_file.name,
        "bytes": data,
        "document_id": str(uuid.uuid4()),
        "folder": "",
        "tags": [],
    }]

    new_chunks, new_metadata, errors = rag.build_documents(files)
    if not new_chunks:
        details = " | ".join(errors) if errors else "No text extracted."
        raise ValueError(f"No chunks created. {details}")

    index, chunks, metadata = rag.load_vectorstore(
        user_id=user_id,
        collection_id=collection_id,
    )

    index, chunks, metadata = rag.add_to_vectorstore(
        index,
        chunks,
        metadata,
        new_chunks,
        new_metadata,
        user_id=user_id,
        collection_id=collection_id,
    )

    safe_name = re.sub(
        r"[^a-zA-Z0-9._-]",
        "_",
        Path(uploaded_file.name).name,
    )
    folder = UPLOAD_DIR / user_id / collection_id
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / f"{uuid.uuid4().hex}_{safe_name}"
    destination.write_bytes(data)

    logger.info(
        "Uploaded %s user=%s collection=%s chunks=%s",
        uploaded_file.name,
        user_id,
        collection_id,
        len(new_chunks),
    )

    return {
        "chunks_added": len(new_chunks),
        "warnings": errors,
    }, destination


def stream_rag_answer(question, user_id, collection_id, top_k):
    index, chunks, metadata = rag.load_vectorstore(
        user_id=user_id,
        collection_id=collection_id,
    )

    if index is None or not chunks:
        return iter([{
            "text": (
                "No indexed documents found. "
                "Please upload a document first."
            ),
            "sources": [],
        }])

    results, token_stream = rag.ask_rag_stream(
        query=question,
        index=index,
        chunks=chunks,
        metadata=metadata,
        chat_history=None,
        top_k=top_k,
        min_similarity=None,
    )

    def answer_items():
        yield {"text": "", "sources": results}
        for piece in token_stream:
            yield {"text": piece, "sources": None}

    return answer_items()


def unpack_stream_item(item):
    if isinstance(item, str):
        return item, None

    if isinstance(item, dict):
        token = (
            item.get("token")
            or item.get("text")
            or item.get("content")
            or ""
        )
        return str(token), item.get("sources")

    if isinstance(item, tuple):
        text_part = str(item[0]) if item else ""
        sources = item[1] if len(item) > 1 else None
        return text_part, sources

    return str(item), None


# ---------------- ADMIN ----------------

def is_admin(user):
    return bool(user and user.get("role") == "admin")


def admin_dashboard():
    st.subheader("Admin dashboard")

    with db() as con:
        user_count = con.execute(
            "SELECT COUNT(*) AS n FROM users"
        ).fetchone()["n"]
        collection_count = con.execute(
            "SELECT COUNT(*) AS n FROM collections"
        ).fetchone()["n"]
        chat_count = con.execute(
            "SELECT COUNT(*) AS n FROM chats"
        ).fetchone()["n"]
        feedback_count = con.execute(
            "SELECT COUNT(*) AS n FROM feedback"
        ).fetchone()["n"]
        events = con.execute("""
            SELECT event_type, COUNT(*) AS n
            FROM events GROUP BY event_type ORDER BY n DESC
        """).fetchall()
        recent = con.execute("""
            SELECT event_type, detail, created_at
            FROM events ORDER BY created_at DESC LIMIT 50
        """).fetchall()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Users", user_count)
    c2.metric("Collections", collection_count)
    c3.metric("Chats", chat_count)
    c4.metric("Feedback", feedback_count)

    st.markdown("#### Event counts")
    if events:
        st.dataframe(
            [{"Event": r["event_type"], "Count": r["n"]} for r in events],
            width="stretch",
        )
    else:
        st.info("No events recorded yet.")

    st.markdown("#### Recent events")
    if recent:
        st.dataframe([dict(r) for r in recent], width="stretch")


# ---------------- LOGIN SCREEN ----------------

def login_screen():
    st.title("📄 AskDoc")
    st.caption("Your private document assistant")

    login_tab, register_tab = st.tabs(["Sign in", "Create account"])

    with login_tab:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button(
                "Sign in",
                width="stretch",
            )

        if submitted:
            try:
                user = login_user(username, password)
                if user:
                    st.session_state.user = user
                    st.session_state.active_chat = None
                    st.session_state.active_collection = None
                    log_event(user["id"], "login")
                    st.rerun()
                else:
                    st.error("Invalid username or password.")
            except Exception as exc:
                record_error("Login", exc)
                st.error(f"Login error: {exc}")

    with register_tab:
        with st.form("register_form"):
            new_username = st.text_input("Choose username")
            new_password = st.text_input(
                "Choose password",
                type="password",
            )
            confirm_password = st.text_input(
                "Confirm password",
                type="password",
            )
            submitted = st.form_submit_button(
                "Create account",
                width="stretch",
            )

        if submitted:
            if new_password != confirm_password:
                st.error("Passwords do not match.")
            else:
                try:
                    ok, message = register_user(
                        new_username,
                        new_password,
                    )
                    if ok:
                        st.success(message)
                    else:
                        st.error(message)
                except Exception as exc:
                    record_error("Registration", exc)
                    st.error(f"Registration error: {exc}")


# ---------------- MAIN APP ----------------

def main_app(user):
    user_id = user["id"]

    with st.sidebar:
        st.title("📄 AskDoc")
        st.caption(f"Signed in as **{user['username']}**")

        if st.button("Sign out", width="stretch"):
            log_event(user_id, "logout")
            st.session_state.clear()
            st.rerun()

        st.divider()
        st.subheader("Document collections")

        collections = get_collections(user_id)
        if not collections:
            st.warning("Create a collection to get started.")
            st.stop()

        collection_map = {row["name"]: row["id"] for row in collections}
        names = list(collection_map.keys())

        current_id = st.session_state.get("active_collection")
        current_name = next(
            (name for name, cid in collection_map.items()
             if cid == current_id),
            names[0],
        )

        selected_name = st.selectbox(
            "Select collection",
            names,
            index=names.index(current_name),
        )
        collection_id = collection_map[selected_name]
        st.session_state.active_collection = collection_id

        with st.expander("＋ Create collection"):
            with st.form("new_collection_form"):
                collection_name = st.text_input("Collection name")
                create_clicked = st.form_submit_button("Create")

            if create_clicked:
                try:
                    ok, msg = create_collection(user_id, collection_name)
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
                except Exception as exc:
                    record_error("Create collection", exc)
                    st.error(f"Could not create collection: {exc}")

        if len(collections) > 1:
            with st.expander("Delete selected collection"):
                st.warning(
                    "This removes its chat history, uploaded files, "
                    "and vector index."
                )
                confirm_delete = st.checkbox(
                    "I understand",
                    key="confirm_delete_collection",
                )
                if st.button(
                    "Delete collection",
                    disabled=not confirm_delete,
                ):
                    try:
                        if delete_collection(user_id, collection_id):
                            st.session_state.active_collection = None
                            st.session_state.active_chat = None
                            st.rerun()
                    except Exception as exc:
                        record_error("Delete collection", exc)
                        st.error(f"Could not delete collection: {exc}")

        st.divider()
        st.subheader("Chat history")
        chats = get_chats(user_id, collection_id)

        if st.button("＋ New chat", width="stretch"):
            try:
                chat_id = create_chat(user_id, collection_id)
                st.session_state.active_chat = chat_id
                st.rerun()
            except Exception as exc:
                record_error("Create chat", exc)
                st.error(f"Could not create chat: {exc}")

        for chat in chats:
            active = st.session_state.get("active_chat") == chat["id"]
            label = ("● " if active else "") + chat["title"]
            if st.button(
                label,
                key=f"chat_{chat['id']}",
                width="stretch",
            ):
                st.session_state.active_chat = chat["id"]
                st.rerun()

        with st.expander("🗑️ Clear chat history"):
            st.caption(
                "Delete saved chats without removing your documents."
            )
            clear_scope = st.radio(
                "Choose what to clear",
                ["Current collection", "All my collections"],
                key="clear_history_scope",
            )
            if clear_scope == "Current collection":
                st.warning(
                    f"Chats in '{selected_name}' will be permanently deleted."
                )
            else:
                st.warning(
                    "All saved chats across every collection "
                    "will be permanently deleted."
                )

            confirm_clear = st.checkbox(
                "I understand this cannot be undone",
                key="confirm_clear_history",
            )
            if st.button(
                "Clear history now",
                key="clear_history_button",
                type="primary",
                disabled=not confirm_clear,
                width="stretch",
            ):
                scope_id = (
                    collection_id
                    if clear_scope == "Current collection"
                    else None
                )
                try:
                    ok, count = clear_chat_history(user_id, scope_id)
                    if ok:
                        st.session_state.active_chat = None
                        st.session_state.pop("confirm_clear_history", None)
                        st.session_state.pop("clear_history_scope", None)
                        log_event(
                            user_id,
                            "chat_history_cleared",
                            f"scope={clear_scope}; chats={count}",
                        )
                        st.success(
                            f"Chat history cleared. Deleted chats: {count}"
                        )
                        st.rerun()
                    else:
                        st.error("Could not clear chat history.")
                except Exception as exc:
                    record_error("Clear chat history", exc)
                    st.error(f"Could not clear history: {exc}")

        st.divider()
        if is_admin(user):
            if st.button("Admin dashboard", width="stretch"):
                st.session_state.show_admin = not st.session_state.get(
                    "show_admin", False
                )

    if st.session_state.get("show_admin") and is_admin(user):
        admin_dashboard()
        show_error_history()
        return

    st.title("Ask your documents anything")
    st.caption(
        "Upload documents to a collection, then ask questions "
        "grounded in them."
    )

    upload_tab, library_tab = st.tabs([
        "Upload documents",
        "Document library",
    ])

    with upload_tab:
        uploaded_files = st.file_uploader(
            "Choose documents",
            type=sorted(ALLOWED_EXTENSIONS),
            accept_multiple_files=True,
            help=f"Maximum {MAX_FILE_MB} MB per file.",
        )

        if st.button(
            "Process documents",
            disabled=not uploaded_files,
            type="primary",
        ):
            progress = st.progress(0)
            for i, uploaded in enumerate(uploaded_files):
                try:
                    with st.spinner(f"Processing {uploaded.name}..."):
                        result, saved_path = upload_document(
                            uploaded,
                            user_id,
                            collection_id,
                        )

                    st.success(
                        f"Processed: {uploaded.name} "
                        f"({result['chunks_added']} chunks)"
                    )
                    if result["warnings"]:
                        with st.expander(f"Warnings: {uploaded.name}"):
                            st.write(result["warnings"])

                    log_event(user_id, "document_upload", uploaded.name)

                except Exception as exc:
                    record_error(
                        f"Upload document: {uploaded.name}",
                        exc,
                    )
                    st.error(f"{uploaded.name}: {exc}")
                    log_event(
                        user_id,
                        "document_error",
                        f"{uploaded.name}: {exc}",
                    )

                progress.progress((i + 1) / len(uploaded_files))

    with library_tab:
        st.markdown(f"**Collection:** {selected_name}")

        library_notice = st.session_state.pop("library_notice", None)
        if library_notice:
            if library_notice[0] == "success":
                st.success(library_notice[1])
            else:
                st.error(library_notice[1])

        try:
            index, chunks, metadata = rag.load_vectorstore(
                user_id=user_id,
                collection_id=collection_id,
            )
            if index is None or not chunks:
                st.info("No indexed documents found.")
            else:
                docs = rag.list_collection_documents(chunks, metadata)
                if docs:
                    st.dataframe(
                        docs,
                        width="stretch",
                        hide_index=True,
                    )

                    # -------- DELETE ONE DOCUMENT --------

                    with st.expander("Delete a document"):
                        doc_ids = [d["document_id"] for d in docs]
                        doc_names = {
                            d["document_id"]: d["source"] for d in docs
                        }
                        delete_key = f"delete_doc_{collection_id}"
                        confirm_doc_key = f"confirm_delete_doc_{collection_id}"

                        doc_to_delete = st.selectbox(
                            "Select document",
                            doc_ids,
                            format_func=lambda x: doc_names.get(x, x),
                            key=delete_key,
                        )
                        st.warning(
                            "This removes the document's chunks from the "
                            "search index. The original uploaded file and "
                            "your chats are not deleted."
                        )
                        confirm_doc_delete = st.checkbox(
                            "I understand",
                            key=confirm_doc_key,
                        )
                        if st.button(
                            "Delete document",
                            key=f"delete_doc_button_{collection_id}",
                            disabled=not confirm_doc_delete,
                        ):
                            try:
                                if not require_collection_owner(
                                    user_id, collection_id
                                ):
                                    raise ValueError(
                                        "Collection not found."
                                    )

                                _, _, _, removed = (
                                    rag.delete_document_from_vectorstore(
                                        index,
                                        chunks,
                                        metadata,
                                        doc_to_delete,
                                        user_id=user_id,
                                        collection_id=collection_id,
                                    )
                                )
                                log_event(
                                    user_id,
                                    "document_deleted",
                                    f"{doc_names.get(doc_to_delete, '')}; "
                                    f"chunks={removed}",
                                )
                                st.session_state.pop(confirm_doc_key, None)
                                st.session_state.pop(delete_key, None)
                                st.session_state.library_notice = (
                                    "success",
                                    f"Deleted '{doc_names.get(doc_to_delete, '')}' "
                                    f"({removed} chunks removed).",
                                )
                                st.rerun()
                            except Exception as exc:
                                record_error("Delete document", exc)
                                st.error(f"Could not delete document: {exc}")

                    # -------- CLEAR ALL DOCUMENTS HISTORY --------

                    with st.expander("🗑️ Clear all documents history"):
                        st.caption(
                            "Remove every indexed document from this "
                            "collection. Uploaded files and chat history "
                            "are kept."
                        )
                        st.warning(
                            f"All documents in '{selected_name}' will be "
                            "removed from the search index. "
                            "You will need to upload them again to ask "
                            "questions about them."
                        )
                        confirm_clear_docs_key = (
                            f"confirm_clear_docs_{collection_id}"
                        )
                        confirm_clear_docs = st.checkbox(
                            "I understand this cannot be undone",
                            key=confirm_clear_docs_key,
                        )
                        if st.button(
                            "Clear all documents now",
                            key=f"clear_docs_button_{collection_id}",
                            type="primary",
                            disabled=not confirm_clear_docs,
                        ):
                            try:
                                if not require_collection_owner(
                                    user_id, collection_id
                                ):
                                    raise ValueError(
                                        "Collection not found."
                                    )

                                rag.delete_vectorstore(
                                    user_id=user_id,
                                    collection_id=collection_id,
                                )
                                log_event(
                                    user_id,
                                    "documents_cleared",
                                    f"collection={selected_name}; "
                                    f"documents={len(docs)}",
                                )
                                st.session_state.pop(
                                    confirm_clear_docs_key, None
                                )
                                st.session_state.library_notice = (
                                    "success",
                                    "All documents cleared from "
                                    f"'{selected_name}'.",
                                )
                                st.rerun()
                            except Exception as exc:
                                record_error("Clear all documents", exc)
                                st.error(f"Could not clear documents: {exc}")
                else:
                    st.info("No indexed documents found.")
        except Exception as exc:
            record_error("Load document library", exc)
            st.warning(f"Could not load document list: {exc}")

    st.divider()

    # -------- ACTIVE CHAT --------

    active_chat = st.session_state.get("active_chat")
    chats = get_chats(user_id, collection_id)
    valid_chat_ids = {row["id"] for row in chats}

    if active_chat not in valid_chat_ids:
        try:
            active_chat = create_chat(user_id, collection_id)
            st.session_state.active_chat = active_chat
        except Exception as exc:
            record_error("Initialize active chat", exc)
            st.error(f"Could not initialize chat: {exc}")
            show_error_history()
            return

    messages = get_messages(active_chat)

    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["sources"]:
                try:
                    sources = json.loads(message["sources"])
                    if sources:
                        with st.expander("Sources"):
                            st.json(sources)
                except Exception:
                    pass

    prompt = st.chat_input("Ask a question about your documents...")

    if prompt:
        prompt = prompt.strip()
        if not prompt:
            st.stop()

        try:
            save_message(active_chat, "user", prompt)
            if len(messages) == 0:
                set_chat_title(active_chat, prompt[:60])
        except Exception as exc:
            record_error("Save user question", exc, prompt)
            st.error(f"Could not save your question: {exc}")
            show_error_history()
            return

        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            answer_box = st.empty()
            status = st.empty()
            full_answer = ""
            sources = None

            try:
                status.caption("Searching your documents...")

                stream = stream_rag_answer(
                    prompt,
                    user_id,
                    collection_id,
                    top_k=4,
                )

                for item in stream:
                    chunk, item_sources = unpack_stream_item(item)
                    if item_sources is not None:
                        sources = item_sources
                    if chunk:
                        full_answer += chunk
                        answer_box.markdown(full_answer + "▌")

                status.empty()

                if not full_answer.strip():
                    full_answer = (
                        "The model returned an empty answer. "
                        "Check Error History and the terminal logs."
                    )
                    answer_box.markdown(full_answer)

                if sources is not None:
                    st.caption(f"Retrieved chunks: {len(sources)}")
                    if sources:
                        with st.expander("Sources and retrieved context"):
                            st.json(sources)

                save_message(
                    active_chat,
                    "assistant",
                    full_answer,
                    sources=sources,
                )
                log_event(user_id, "question_answered")
                st.session_state.last_feedback_chat = active_chat

            except Exception as exc:
                status.empty()
                record_error("Answer generation / RAG", exc, prompt)
                st.error(f"Could not answer: {exc}")
                answer_box.markdown(
                    "I couldn't generate an answer. "
                    "The full error is saved below in Error History."
                )
                log_event(user_id, "answer_error", str(exc))

                try:
                    save_message(
                        active_chat,
                        "assistant",
                        f"Answer failed: {exc}",
                    )
                except Exception as save_exc:
                    record_error(
                        "Save answer error message",
                        save_exc,
                        prompt,
                    )

        st.rerun()

    # -------- FEEDBACK --------

    st.divider()
    st.subheader("Was this answer helpful?")
    f1, f2 = st.columns(2)

    with f1:
        if st.button("👍 Helpful", width="stretch"):
            try:
                with db() as con:
                    con.execute("""
                        INSERT INTO feedback
                        (id, user_id, chat_id, rating, comment, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        str(uuid.uuid4()),
                        user_id,
                        active_chat,
                        "helpful",
                        "",
                        datetime.now().isoformat(timespec="seconds"),
                    ))
                log_event(user_id, "feedback_helpful")
                st.success("Thanks for your feedback.")
            except Exception as exc:
                record_error("Save helpful feedback", exc)
                st.error(f"Could not save feedback: {exc}")

    with f2:
        if st.button("👎 Not helpful", width="stretch"):
            try:
                with db() as con:
                    con.execute("""
                        INSERT INTO feedback
                        (id, user_id, chat_id, rating, comment, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        str(uuid.uuid4()),
                        user_id,
                        active_chat,
                        "not_helpful",
                        "",
                        datetime.now().isoformat(timespec="seconds"),
                    ))
                log_event(user_id, "feedback_not_helpful")
                st.success("Thanks for your feedback.")
            except Exception as exc:
                record_error("Save not-helpful feedback", exc)
                st.error(f"Could not save feedback: {exc}")

    # Keep error panel visible at the bottom of the page.
    show_error_history()


# ---------------- START ----------------

init_error_history()

try:
    init_db()
except Exception as exc:
    record_error("Database initialization", exc)
    st.error(f"Database initialization failed: {exc}")
    show_error_history()
    st.stop()

if "user" not in st.session_state:
    login_screen()
    show_error_history()
else:
    main_app(st.session_state.user)