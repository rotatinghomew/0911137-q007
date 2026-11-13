FROM python:3.11-slim-bookworm@sha256:65a93d69fa75478d554f4ad27c85c1e69fa184956261b4301ebaf6dbb0a3543d
WORKDIR /app
COPY requirements-build.txt requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements-build.txt -r requirements.txt
COPY . .
RUN python -m pip install --no-build-isolation --no-deps -e .
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "-m", "choice_lab", "--host", "0.0.0.0", "--port", "8080"]
