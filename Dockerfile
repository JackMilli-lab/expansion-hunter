FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home cockpit
COPY config ./config
RUN mkdir /app/data /app/reports && chown -R cockpit:cockpit /app/data /app/reports
USER cockpit
EXPOSE 8000
ENTRYPOINT ["expansion-hunter"]
CMD ["app", "--host", "0.0.0.0", "--port", "8000"]
