FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PORT=80 \
    MUTABLE_FLIP_AFTER=4

WORKDIR /srv/site

COPY server.py ./
COPY html/ ./html/
COPY mutable/ ./mutable/

EXPOSE 80
CMD ["python", "server.py"]
