FROM python:3.11-slim-bookworm@sha256:528257d48c1da0dcecc2e725d1ae34498d60c965f1241e39cd6a85a8859bdf84
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY importers/ importers/
ENV PYTHONUNBUFFERED=1 FINANCE_DATA=/finance
CMD ["python", "-m", "importers.simplefin.schedule"]
