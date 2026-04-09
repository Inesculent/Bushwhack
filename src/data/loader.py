from datasets import load_dataset
from huggingface_hub import login
import os
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("HUGGINGFACE_HUB_TOKEN")

if token:
    login(token=token)
else:
    print("Warning: HUGGINGFACE_HUB_TOKEN not found in environment variables.")

ds1 = "ronantakizawa/github-codereview"
ds2 = "Alibaba-Aone/aacr-bench"
ds3 = "foundry-ai/swe-prbench"

dataset = ds3



if dataset == "ronantakizawa/github-codereview":
    ds = load_dataset(dataset)

    keys = ds["train"].features.keys()
    print(f"Dataset keys: {keys}")

    python_reviews = ds['train'].filter(lambda x: x['language'] == "Python")
    print(f"Found {len(python_reviews)} Python reviews in the dataset.")
    record = python_reviews[0]
    output_string = "\n".join(f"{key}: {value}" for key, value in record.items())
    print(output_string)

elif dataset == "Alibaba-Aone/aacr-bench":
    ds = load_dataset(dataset)

    keys = ds["train"].features.keys()
    print(f"Dataset keys: {keys}")
    python_reviews = ds['train'].filter(lambda x: x['project_main_language'] == "Python")
    print(f"Found {len(python_reviews)} Python reviews in the dataset.")
    record = python_reviews[0]
    output_string = "\n".join(f"{key}: {value}" for key, value in record.items())
    print(output_string)

elif dataset == "foundry-ai/swe-prbench":
    ds = load_dataset(dataset,'prs')

    keys = ds["train"].features.keys()
    print(f"Dataset keys: {keys}")
    python_reviews = ds['train'].filter(lambda x: x.get('language') == "Python")
    print(f"Found {len(python_reviews)} Python reviews in the dataset.")
    record = python_reviews[0]
    keys_to_print = ["task_id", "repo", "language", "difficulty", "rvs_score", "base_commit", "head_commit", "pr_url"]
    
    output_lines = []
    for key in keys_to_print:
        value = record.get(key, "Key not found") 
        output_lines.append(f"{key}: {value}")
    print("\n".join(output_lines))