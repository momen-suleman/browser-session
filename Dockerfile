# Playwright's own image: Chromium plus every system library it needs.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY browser_session ./browser_session
RUN mkdir -p /app/data && chown -R pwuser:pwuser /app/data
USER pwuser
ENV PYTHONUNBUFFERED=1 PORT=9321 BROWSER_DATA_DIR=/app/data
EXPOSE 9321
CMD ["python", "-m", "browser_session"]
