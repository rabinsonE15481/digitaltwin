# AWS Lambda container image for the twin brain (light: no PyTorch, no vector DB)
FROM public.ecr.aws/lambda/python:3.12

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py brain.py persona.md chapters.json ${LAMBDA_TASK_ROOT}/
COPY kb/ ${LAMBDA_TASK_ROOT}/kb/

CMD ["main.handler"]
