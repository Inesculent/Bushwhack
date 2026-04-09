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

dsSet = {ds1, ds2, ds3}




if ds1 in dsSet:
    ds = load_dataset(ds1)

    print("Loaded dataset:", ds1)

    keys = ds["train"].features.keys()
    print(f"Dataset keys: {keys}")

    python_reviews = ds['train'].filter(lambda x: x['language'] == "Python")
    print(f"Found {len(python_reviews)} Python reviews in the dataset.")
    record = python_reviews[0]
    output_string = "\n".join(f"{key}: {value}" for key, value in record.items())
    print(output_string)


    print('=' * 40 + "End of ds1 sample record" + '=' * 40)

if ds2 in dsSet:
    ds = load_dataset(ds2)
    print("Loaded dataset:", ds2)
    keys = ds["train"].features.keys()
    print(f"Dataset keys: {keys}")
    python_reviews = ds['train'].filter(lambda x: x['project_main_language'] == "Python")
    print(f"Found {len(python_reviews)} Python reviews in the dataset.")
    record = python_reviews[0]
    output_string = "\n".join(f"{key}: {value}" for key, value in record.items())
    print(output_string)

    print('=' * 40 + "End of ds2 sample record" + '=' * 40)

if ds3 in dsSet:
    ds = load_dataset(ds3,'prs')
    print("Loaded dataset:", ds3)
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
    print('=' * 40 + "End of ds3 sample record" + '=' * 40)