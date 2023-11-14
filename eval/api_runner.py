from typing import Optional
from eval.eval import compare_query_results
import pandas as pd
from utils.pruning import prune_metadata_str
from utils.questions import prepare_questions_df
from tqdm import tqdm
from time import time
import requests

def generate_prompt(prompt_file, question, db_name, public_data):
    with open(prompt_file, "r") as f:
        prompt = f.read()

    pruned_metadata_str = prune_metadata_str(question, db_name, public_data)
    prompt = prompt.format(
        user_question=question, table_metadata_string=pruned_metadata_str
    )
    return prompt

def run_api_eval(args):
    # get params from args
    questions_file = args.questions_file
    prompt_file = args.prompt_file
    num_questions = args.num_questions
    public_data = not args.use_private_data
    api_url = args.url
    output_file = args.output_file
    num_beams = args.num_beams

    print("preparing questions...")
    # get questions
    print(f"Using {num_questions} questions from {questions_file}")
    df = prepare_questions_df(questions_file, num_questions)

    # create a prompt for each question
    df["prompt"] = df[["question", "db_name"]].apply(
        lambda row: generate_prompt(
            prompt_file, row["question"], row["db_name"], public_data
        ),
        axis=1,
    )

    print("questions prepared\nnow loading model...")
    # initialize tokenizer and model
    total_tried = 0
    total_correct = 0
    output_rows = []

    with tqdm(total=len(df)) as pbar:
        for row in df.to_dict("records"):
            total_tried += 1
            start_time = time()
            # we set return_full_text to False so that we don't get the prompt text in the generated text
            # this simplifies our postprocessing to deal with just the truncation of the end of the query
            r = requests.post(api_url, json={
                "prompt": row["prompt"],
                "use_beam_search": True,
                "n": 1,
                "best_of": num_beams,
                "temperature": 0,
                "stop": [";", "```"],
                "max_tokens": 600
            })
            end_time = time()
            generated_query = r.json()['text'][0].split("```")[-1].split("```")[0].split(";")[0].strip() + ";"

            row["generated_query"] = generated_query
            row["latency_seconds"] = end_time - start_time
            golden_query = row["query"]
            db_name = row["db_name"]
            question = row["question"]
            query_category = row["query_category"]
            exact_match = correct = 0
            db_creds = {
                "host": "localhost",
                "port": 5432,
                "user": "postgres",
                "password": "postgres",
                "database": db_name,
            }

            try:
                exact_match, correct = compare_query_results(
                    query_gold=golden_query,
                    query_gen=generated_query,
                    db_name=db_name,
                    db_creds=db_creds,
                    question=question,
                    query_category=query_category,
                )
                row["exact_match"] = int(exact_match)
                row["correct"] = int(correct)
                row["error_msg"] = ""
                if correct:
                    total_correct += 1
            except Exception as e:
                row["error_db_exec"] = 1
                row["error_msg"] = f"QUERY EXECUTION ERROR: {e}"

            output_rows.append(row)
            pbar.update(1)
            pbar.set_description(
                f"Correct so far: {total_correct}/{total_tried} ({100*total_correct/total_tried:.2f}%)"
            )

    output_df = pd.DataFrame(output_rows)
    del output_df["prompt"]
    print(output_df.groupby("query_category")[["exact_match", "correct"]].mean())
    output_df = output_df.sort_values(by=["db_name", "query_category", "question"])
    output_df.to_csv(output_file, index=False, float_format="%.2f")
