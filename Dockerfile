FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-server \
    git \
    tmux \
    curl \
    vim-tiny \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Configure sshd
RUN mkdir -p /run/sshd && \
    sed -i 's/#PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config && \
    sed -i 's/#PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && \
    sed -i 's/#PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config && \
    echo "AllowUsers botuser" >> /etc/ssh/sshd_config && \
    # Use persistent host keys from mounted volume
    echo "HostKey /etc/ssh/ssh_host_keys/ssh_host_ed25519_key" >> /etc/ssh/sshd_config && \
    echo "HostKey /etc/ssh/ssh_host_keys/ssh_host_rsa_key" >> /etc/ssh/sshd_config

# Create bot user
RUN useradd -m -s /bin/bash botuser && \
    mkdir -p /home/botuser/.ssh && \
    chmod 700 /home/botuser/.ssh && \
    chown -R botuser:botuser /home/botuser/.ssh

# Create venv directory (separate from host .venv)
RUN mkdir -p /opt/bot-venv && chown botuser:botuser /opt/bot-venv

# Set environment variables
ENV UV_PROJECT_ENVIRONMENT=/opt/bot-venv
ENV OLLAMA_HOST=http://host.docker.internal:11434

# Seed uv dependency cache
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync && rm -rf pyproject.toml uv.lock

# Install botctl
COPY docker/botctl.sh /usr/local/bin/botctl
RUN chmod +x /usr/local/bin/botctl

# Install entrypoint
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 22

ENTRYPOINT ["/entrypoint.sh"]
