# Blast Radius entry. The digest is the image the organizers labeled commits in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

# ref.tar.gz: sqlglot's Python sources and tests at the pinned commit (MIT, see its LICENSE).
# index.pkl.gz: the coverage map built offline from that commit (see tools/).
COPY selector.py index.pkl.gz ref.tar.gz /app/
RUN mkdir -p /opt/sqlglot \
 && python -c "import tarfile; tarfile.open('/app/ref.tar.gz').extractall('/opt/sqlglot', filter='data')" \
 && rm /app/ref.tar.gz \
 && chmod -R a+rX /app /opt/sqlglot

ENV BR_SRC=/opt/sqlglot \
    PYTHONDONTWRITEBYTECODE=1

CMD ["python", "/app/selector.py"]
