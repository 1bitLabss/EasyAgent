//! Scaffold for an iOS or Android shell. Desktop builds do not compile this.
//!
//! The shell loads the same page the computer serves and pairs with the same
//! token. It does not keep a copy of the chats. Build steps are in desktop/MOBILE.md.

pub fn shell_note() -> &'static str {
    "Load the computer's EasyAgent page and pair with the same token. Chats stay on that computer."
}
