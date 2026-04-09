FROM python:3.11-slim

# Install system tools
RUN apt-get update && apt-get install -y \
    ripgrep \
    git \
    tini \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Install MCP runtime and Tree-sitter bindings.
RUN pip install mcp tree-sitter tree-sitter-language-pack

# Bundle the local MCP server implementation in the image.
COPY server.py /opt/fs-mcp/server.py

# Create a directory for grammars if you plan to compile custom ones, 
# though 'tree-sitter-languages' handles the common ones automatically.
WORKDIR /repo

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tail", "-f", "/dev/null"]