"""Request and response shapes.

Separate from the database models on purpose. A model describes what is stored;
a schema describes what crosses the wire. Returning a model directly would
publish every column - including the password hash - and tie the API's shape to
the database's, so a rename in one becomes a breaking change in the other.
"""
