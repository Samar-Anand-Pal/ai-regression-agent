# Membership

**Owner:** Front desk · **Status:** Approved

## User stories

- As a visitor, I want to sign up with my name and email so that I can borrow books.
- As a librarian, I want to suspend a member who breaks the rules, and reinstate them later.

## Requirements

- **REQ-010** A visitor can register as a member with a name and an email address. A new member starts out `ACTIVE`.
- **REQ-011** Registering with an email address that already belongs to a member is rejected with `ALREADY_EXISTS`.
- **REQ-012** A member's profile can be looked up by member ID. An unknown ID returns `NOT_FOUND`.
- **REQ-013** A librarian can suspend a member, giving a reason. The member's status becomes `SUSPENDED`.
- **REQ-014** A suspended member cannot borrow books: a checkout attempt by a suspended member is rejected with `PERMISSION_DENIED`.
- **REQ-015** A librarian can reinstate a suspended member, which sets their status back to `ACTIVE`, after which they can borrow again.
- **REQ-016** A member can change their email address to a new one.
- **REQ-017** Changing an email address to one that already belongs to another member is rejected with `ALREADY_EXISTS`.

## Out of scope for the API

- New members receive a welcome email with their library card number.
