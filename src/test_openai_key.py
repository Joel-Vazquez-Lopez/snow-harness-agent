import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.environ["OPENAI_API_KEY"]
base_url = os.environ["OPENAI_BASE_URL"]
model = os.environ["OPENAI_MODEL"]

client = OpenAI(
    api_key=api_key,
    base_url=base_url,
)

response = client.chat.completions.create(
    model=model,
    messages=[
        {"role": "user", "content": "Say only: API key works."}
    ],
    temperature=0,
)

print(response.choices[0].message.content)