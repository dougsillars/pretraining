"""Tool to periodically run benchmarks on the best model of the subnet and some well-known models."""

import abc
from argparse import ArgumentParser
import asyncio
import time
from typing import Dict, List, Tuple
import requests
import wandb
import torch
import random
from tqdm import tqdm
from model.data import ModelId
from model.storage.chain.chain_model_metadata_store import ChainModelMetadataStore
from model.storage.hugging_face.hugging_face_model_store import HuggingFaceModelStore
import pretrain as pt
from datasets import load_dataset
from transformers import AutoTokenizer, AutoModelForCausalLM
from torch.nn import CrossEntropyLoss
from collections import defaultdict
import os
import wandb
import pandas as pd
from dotenv import load_dotenv
import bittensor as bt
import constants

from pretrain.graph import best_uid

load_dotenv()  # take environment variables from .env.

PROJECT = "pretraining-leaderboard-data"
ENTITY = "raofoundation"
WANDB_TOKEN = os.getenv("WANDB_API_KEY")


def compute_ppl(
    text,
    model: AutoModelForCausalLM,
    tokenizer: AutoTokenizer,
    stride: int = 512,
    max_length: int = 1024,
    device=None,
) -> float:
    """Returns the perplexity of the model on the given text."""

    if device is not None:
        assert device in ["gpu", "cpu", "cuda"], "device should be either gpu or cpu."
        if device == "gpu":
            device = "cuda"
    else:
        device = "cuda" if torch.cuda.is_available() else "cpu"

    model = model.to(device)

    encodings = tokenizer(
        text,
        truncation=False,
        return_tensors="pt",
    ).to(device)

    loss_fct = CrossEntropyLoss(reduction="none", ignore_index=-100)
    seq_len = encodings.input_ids.size(1)

    losses = []
    prev_end_loc = 0
    for begin_loc in tqdm(range(0, seq_len, stride)):
        end_loc = min(begin_loc + max_length, seq_len)
        # Computes how much overlap there is with the previous batch.
        new_tokens_len = end_loc - prev_end_loc
        if end_loc - begin_loc < max_length:
            bt.logging.info(
                f"Skipping batch as it has less than max_length tokens: {begin_loc}:{end_loc}."
            )
            break
        input_ids = encodings.input_ids[:, begin_loc:end_loc].to(device)
        # attn_mask = encodings.attention_mask[:, begin_loc:end_loc].to(device)
        labels = input_ids.clone()
        # Ignore the tokens we've processed on a previous batch. -100 is a magic
        # value that is ignored by the CrossEntropyLoss function
        labels[:, :-new_tokens_len] = -100

        with torch.no_grad():
            out_logits = model(input_ids).logits

        # Shift by 1 token.
        shift_logits = out_logits[..., :-1, :].contiguous()
        shift_labels = labels[..., 1:].contiguous()

        # Flatten the tensors
        shift_logits = shift_logits.view(-1, model.config.vocab_size)
        shift_labels = shift_labels.view(-1)
        losses.append(loss_fct(shift_logits, shift_labels))
    return torch.exp(torch.stack(losses).mean()).item()


class ModelProvider(abc.ABC):
    """Base class for a provider of a model and its tokenizer."""

    @abc.abstractmethod
    def get_model(self) -> AutoModelForCausalLM:
        pass

    @abc.abstractmethod
    def get_tokenizer(self) -> AutoTokenizer:
        pass


class HuggingFaceModelProvider(ModelProvider):
    """Provides a well-known model from hugging face."""

    def __init__(self, model_name: str, cache_dir: str):
        self.model_name = model_name
        self.cache_dir = cache_dir

    def get_model(self) -> AutoModelForCausalLM:
        return AutoModelForCausalLM.from_pretrained(
            self.model_name, cache_dir=self.cache_dir
        )

    def get_tokenizer(self) -> AutoTokenizer:
        return AutoTokenizer.from_pretrained(self.model_name, cache_dir=self.cache_dir)


class SubnetModelProvider(ModelProvider):
    """Provides models from the subnet."""

    def __init__(self, model_id: ModelId, cache_dir: str):
        self.model_id = model_id
        self.cache_dir = cache_dir

    def get_model(self) -> AutoModelForCausalLM:
        store = HuggingFaceModelStore()
        model = asyncio.run(store.download_model(self.model_id, self.cache_dir))
        return model.pt_model

    def get_tokenizer(self) -> AutoTokenizer:
        return pt.model.get_tokenizer(cache_dir=self.cache_dir)


def get_best_model_provider(cache_dir: str) -> Tuple[str, SubnetModelProvider]:
    """Returns a provider to fetch the subnets best model.

    Returns:
        Tuple[str, SubnetModelProvider]: A tuple containing the models' HF repo and the model provider.
    """
    metagraph = bt.metagraph(netuid=constants.SUBNET_UID)
    subtensor = bt.subtensor("finney")
    best_uid = pt.graph.best_uid(metagraph=metagraph)
    hotkey = metagraph.hotkeys[best_uid]

    metagraph_store = ChainModelMetadataStore(subtensor)
    metadata = asyncio.run(metagraph_store.retrieve_model_metadata(hotkey))
    if metadata is None:
        raise ValueError(f"No model metadata found for miner {best_uid}")

    return (
        f"{metadata.id.namespace}/{metadata.id.name}",
        SubnetModelProvider(metadata.id, cache_dir),
    )


def get_wikitext103(cache_dir: str) -> str:
    """Returns the wikitext103 dataset.

    Args:
        cache_dir (str): The directory to cache the dataset.
    """
    wikitext_dataset = load_dataset(
        "wikitext", "wikitext-103-raw-v1", split="test", cache_dir=cache_dir
    )
    return "\n\n".join(wikitext_dataset["text"])


def get_lambada(cache_dir: str) -> str:
    """Returns the lambada dataset.

    Args:
        cache_dir (str): The directory to cache the dataset.
    """
    lambada_dataset = load_dataset("lambada", split="test", cache_dir=cache_dir)
    return "\n\n".join(lambada_dataset["text"])


def get_ptb(cache_dir: str) -> str:
    """Returns the Penn Treebank dataset.

    Args:
        cache_dir (str): The directory to cache the dataset.
    """
    ptb_dataset = load_dataset("ptb", split="test", cache_dir=cache_dir)
    return "\n\n".join(ptb_dataset["text"])


def get_falcon() -> str:
    """Returns a random subset of text from the Falcon Refined Web dataset."""

    def _fetch_data_for_page(page: int, max_retries: int = 5) -> List[str]:
        params = {
            "dataset": "tiiuae/falcon-refinedweb",
            "config": "default",
            "split": "train",
            "offset": page,
            "limit": 100,
        }

        attempt = 0
        while attempt < max_retries:
            try:
                response = requests.get(
                    "https://datasets-server.huggingface.co/rows",
                    params=params,
                    timeout=60,
                )
                response.raise_for_status()  # This will raise an HTTPError if the HTTP request returned an unsuccessful status code
                return [r["row"]["content"] for r in response.json()["rows"]]
            except requests.exceptions.RequestException:
                attempt += 1
                bt.logging.warning(
                    f"Failed to fetch data, retrying. Attempt {attempt}/{max_retries}"
                )
                if attempt < max_retries:
                    time.sleep(3)
                else:
                    bt.logging.error(
                        "Maximum retry limit reached. Unable to fetch data."
                    )
                    raise

    pages = [
        random.randint(1, pt.dataset.SubsetFalconLoader.max_pages) for _ in range(20)
    ]
    rows = []
    for page in pages:
        rows.extend(_fetch_data_for_page(page))
    return "\n\n".join(rows)


def format_model_size(size: int) -> str:
    """Formats a model size into a human readable format."""
    if size >= 1e12:
        return f"{size / 1e12:.1f}T"
    if size >= 1e9:
        return f"{size / 1e9:.1f}B"
    if size >= 1e6:
        return f"{int(size // 1e6)}M"
    return str(size)


def run_benchmarks(args: ArgumentParser, datasets: Dict[str, str]):
    """Performs a single run of the benchmarks on the given datasets."""
    best_model_hf, best_model_provider = get_best_model_provider(args.cache_dir)
    models = {
        best_model_hf: best_model_provider,
        "gpt2": HuggingFaceModelProvider("gpt2", args.cache_dir),
        "gpt2-large": HuggingFaceModelProvider("gpt2-large", args.cache_dir),
    }

    ppls = defaultdict(list)
    model_sizes = []
    # First compute for the standard models.
    for model_name, provider in models.items():
        bt.logging.info(f"Computing benchmarks for model: {model_name}")
        model = provider.get_model()
        tokenizer = provider.get_tokenizer()
        for dataset_name, dataset in datasets.items():
            bt.logging.info(
                f"Computing PPL for model: {model_name} on dataset: {dataset_name}"
            )
            ppls[dataset_name].append(compute_ppl(dataset, model, tokenizer))
        model_size = sum(p.numel() for p in model.parameters())
        model_sizes.append(format_model_size(model_size))
        del model
        del tokenizer

    # Log to wandb.
    wandb.login(key=WANDB_TOKEN)
    with wandb.init(project=PROJECT, entity=ENTITY):
        table = wandb.Table(
            dataframe=pd.DataFrame(
                {"Model": models.keys(), "Size": model_sizes, **ppls}
            )
        )
        wandb.log({"benchmarks": table})


def main(args: ArgumentParser):

    datasets = {
        "Wikitext103 (PPL)": get_wikitext103(args.cache_dir),
        "Falcon Refined Web (PPL)": get_falcon(),
    }

    while True:
        try:
            run_benchmarks(args, datasets)

            # Run every 12 hours.
            time.sleep(12 * 60 * 60)
        except Exception as e:
            bt.logging.error(f"Exception occurred: {e}")

            # Try again after 10 minutes.
            time.sleep(10 * 60)


if __name__ == "__main__":
    bt.logging()

    parser = ArgumentParser()
    parser.add_argument("--cache_dir", type=str, default=None)

    args = parser.parse_args()

    main(args)
