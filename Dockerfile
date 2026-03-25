FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (for Docker layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Create data directory for SQLite persistence
RUN mkdir -p /app/data

# Run the bot
CMD ["python", "bot.py"]
