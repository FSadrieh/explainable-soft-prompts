import argparse
from collections import Counter
from typing import List, Tuple
import torch
import csv
import numpy as np

from prompt_token_drop_out import prompt_token_drop_out
from prompt_shortener import validate_prompt
from utils import (
    get_model_embedding_spaces,
    get_model_names_from_numbers,
    load_soft_prompt_weight,
    get_k_nearest_neighbors_for_all_tokens,
    validate_soft_prompt_on_multiple_models,
)


class Token_Relevance_Evaluation:
    def __init__(
        self,
        soft_prompt_name: str,
        model_numbers: List[int],
        config: str,
        accelerator: str,
        prompt_length: int,
        embedding_size: int,
        k: int,
        batch_size: int,
        use_test_set: bool = False,
    ):
        self.soft_prompt_name = soft_prompt_name
        self.model_numbers = model_numbers
        self.model_names = get_model_names_from_numbers(self.model_numbers)
        self.config = config
        self.accelerator = accelerator
        self.prompt_length = prompt_length
        self.embedding_size = embedding_size
        self.k = k
        self.batch_size = batch_size
        self.use_test_set = use_test_set

    def token_relevance_evaluation(self):
        # Check if we have saved the results already
        save_path = f"logs/explainable-soft-prompts/{self.soft_prompt_name}/checkpoints/{'_'.join([str(model_number) for model_number in self.model_numbers])}_evaluation_with_k_{self.k}"
        save_path += "_test.csv" if self.use_test_set else ".csv"
        try:
            self.read_from_csv(path=save_path)
        except FileNotFoundError:
            self.loss_evaluation()
            self.nn_evaluation()
            self.save_results(path=save_path)

    def loss_evaluation(self):
        """
        loss_evaluation() does all loss related evaluations.
        First the important tokens are found using token masking. model_per_token is the important token assignment and model_loss_per_token their importance.
        Then we evaluate the loss of the SP on each model individually.
        Then we evaluate the loss of the SP with the unimportant tokens masked.
        Finally we evaluate the loss of the SP with the prompt shortened.
        """
        self.model_per_token, self.model_loss_per_token = prompt_token_drop_out(
            self.soft_prompt_name,
            self.model_numbers,
            self.config,
            self.accelerator,
            self.prompt_length,
            self.embedding_size,
            self.batch_size,
            self.use_test_set,
        )
        self.individual_model_loss = validate_soft_prompt_on_multiple_models(
            model_numbers=self.model_numbers,
            config_path=self.config,
            accelerator=self.accelerator,
            prompt_length=self.prompt_length,
            batch_size=self.batch_size,
            use_test_set=self.use_test_set,
            soft_prompt_name=self.soft_prompt_name,
        )
        self.masked_loss = []
        self.compressed_loss = []
        for model_number in self.model_numbers:
            # Since we are going to specify inverse true, we keep these tokens
            tokens_to_keep = [j for j, x in enumerate(self.model_per_token) if x == model_number]
            print("Evaluating model", model_number, "with remaining tokens", tokens_to_keep)
            prompt_shorten_args = {
                "model_number": model_number,
                "config": self.config,
                "soft_prompt_name": self.soft_prompt_name,
                "accelerator": self.accelerator,
                "prompt_length": self.prompt_length,
                "embedding_size": self.embedding_size,
                "dropped_out_tokens": tokens_to_keep,
                "inverse": True,
                "shorten": False,
                "batch_size": self.batch_size,
                "use_test_set": self.use_test_set,
            }
            self.masked_loss.append(validate_prompt(**prompt_shorten_args))
            prompt_shorten_args["shorten"] = True
            self.compressed_loss.append(validate_prompt(**prompt_shorten_args))

    def nn_evaluation(self):
        """
        Here we combine the results from the token masking and the nearest neighbor vote. We calculate the alignment between those two.
        """
        model_embedding_spaces, labels = get_model_embedding_spaces(self.model_names, label_type="model_number")
        soft_prompt_weight = load_soft_prompt_weight(self.soft_prompt_name)

        self.euc_nn_votes = self.nearest_neighbor_vote("euclidean", soft_prompt_weight, model_embedding_spaces, labels)
        self.cos_nn_votes = self.nearest_neighbor_vote("cosine", soft_prompt_weight, model_embedding_spaces, labels)

        # Check if neighbor vote model correspond to the model through loss
        self.euc_accuracy = sum(
            self.euc_nn_votes[i][0] == self.model_per_token[i] for i in range(len(self.euc_nn_votes))
        ) / len(self.euc_nn_votes)
        self.cos_accuracy = sum(
            self.cos_nn_votes[i][0] == self.model_per_token[i] for i in range(len(self.cos_nn_votes))
        ) / len(self.cos_nn_votes)

        # These metrics are not reported in the paper since they are more experimental
        self.euc_waccuracy = sum(
            (self.euc_nn_votes[i][0] == self.model_per_token[i]) * self.euc_nn_votes[i][1]
            for i in range(len(self.euc_nn_votes))
        ) / sum(self.euc_nn_votes[i][1] for i in range(len(self.euc_nn_votes)))
        self.cos_waccuracy = sum(
            (self.cos_nn_votes[i][0] == self.model_per_token[i]) * self.cos_nn_votes[i][1]
            for i in range(len(self.cos_nn_votes))
        ) / sum(self.cos_nn_votes[i][1] for i in range(len(self.cos_nn_votes)))
        self.euc_to_cos_accuracy = sum(
            self.euc_nn_votes[i][0] == self.cos_nn_votes[i][0] for i in range(len(self.euc_nn_votes))
        ) / len(self.euc_nn_votes)

    def nearest_neighbor_vote(
        self, distance_metric: str, soft_prompt_weight: torch.Tensor, model_embedding_spaces: list, labels: list
    ) -> List[Tuple[int, float]]:
        """
        Gets the k nearest neighbors for all tokens. They vote for the most common model. The index of the model and the certainty is returned.
        """
        nearest_neighbors = get_k_nearest_neighbors_for_all_tokens(
            distance_metric, soft_prompt_weight, model_embedding_spaces, self.k
        )
        nn_votes = []
        for token_neighbors in nearest_neighbors:
            neighbor_labels = [labels[neighbor] for neighbor in token_neighbors]
            most_common = Counter(neighbor_labels).most_common()[0]
            nn_votes.append((most_common[0], most_common[1] / self.k))

        return nn_votes

    def save_results(self, path: str):
        """
        save_results saves all the results from the evaluation to a csv file. The file structure of the csv is fixed and should not be changed. This allows us to read the results from the csv file in the future.
        """
        with open(
            path,
            "w+",
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["token id"] + [i for i in range(self.prompt_length)])
            writer.writerow(["Loss assignment"] + self.model_per_token)
            writer.writerow(["Euclidean assignment"] + [self.euc_nn_votes[i][0] for i in range(len(self.euc_nn_votes))])
            writer.writerow(["Cosine assignment"] + [self.cos_nn_votes[i][0] for i in range(len(self.cos_nn_votes))])
            writer.writerow(["Loss"] + self.model_loss_per_token)
            writer.writerow(["Euclidean certanty"] + [self.euc_nn_votes[i][1] for i in range(len(self.euc_nn_votes))])
            writer.writerow(["Cosine certanty"] + [self.cos_nn_votes[i][1] for i in range(len(self.cos_nn_votes))])
            writer.writerow([""])
            writer.writerow(["Euclidean accuracy"] + [self.euc_accuracy])
            writer.writerow(["Cosine accuracy"] + [self.cos_accuracy])
            writer.writerow(["Euclidean weighted accuracy"] + [self.euc_waccuracy])
            writer.writerow(["Cosine weighted accuracy"] + [self.cos_waccuracy])
            writer.writerow(["Euclidean to cosine accuracy"] + [self.euc_to_cos_accuracy])
            writer.writerow([""])
            writer.writerow(["Prompt compression"] + self.model_names + ["average"])
            writer.writerow(["Normal prompt length"] + self.masked_loss + [np.mean(self.masked_loss)])
            writer.writerow(["Shortened prompt length"] + self.compressed_loss + [np.mean(self.compressed_loss)])
            writer.writerow(["One model loss"] + self.individual_model_loss + [np.mean(self.individual_model_loss)])

    def read_from_csv(self, path: str):
        """
        If we have already made the calculations we do not need to do them again. We know how the csv is structured. Therefore we can read the values from the csv.
        """
        with open(
            path,
            "r",
        ) as f:
            reader = csv.reader(f)
            rows = [row for row in reader]
            self.model_per_token = rows[1][1:]
            self.euc_nn_votes = rows[2][1:]
            self.cos_nn_votes = rows[3][1:]
            self.model_loss_per_token = rows[4][1:]
            self.model_loss_per_token = [float(loss) for loss in self.model_loss_per_token]
            self.euc_accuracy = float(rows[8][1])
            self.cos_accuracy = float(rows[9][1])
            self.euc_waccuracy = float(rows[10][1])
            self.cos_waccuracy = float(rows[11][1])
            self.euc_to_cos_accuracy = float(rows[12][1])
            self.masked_loss = rows[15][1:]
            self.masked_loss = [float(loss) for loss in self.masked_loss]
            self.compressed_loss = rows[16][1:]
            self.compressed_loss = [float(loss) for loss in self.compressed_loss]
            self.individual_model_loss = rows[17][1:]
            self.individual_model_loss = [float(loss) for loss in self.individual_model_loss]


def arg_parser():
    # Token relevance evaluation should not be called directly, rather called by creat_report.py. For debugging purposes, we allow it to be called directly.
    parser = argparse.ArgumentParser()
    parser.add_argument("soft_prompt_name", type=str)
    parser.add_argument("model_numbers", type=str, help="Comma separated list of model numbers to test on.")
    parser.add_argument("config", type=str, help="path to the config file for the validation")
    parser.add_argument("-a", "--accelerator", type=str, default="cuda", help="Supports: cuda, cpu, tpu, mps")
    parser.add_argument("-p", "--prompt_length", type=int, default=16)
    parser.add_argument("-e", "--embedding_size", type=int, default=768)
    parser.add_argument("-k", "--k", type=int, default=7)
    parser.add_argument("-b", "--batch_size", type=int, default=128)
    return parser.parse_args()


def main():
    args = arg_parser()
    model_numbers = args.model_numbers.split(",")
    evaluator = Token_Relevance_Evaluation(
        args.soft_prompt_name,
        model_numbers,
        args.config,
        args.accelerator,
        args.prompt_length,
        args.embedding_size,
        args.k,
        args.batch_size,
    )
    evaluator.token_relevance_evaluation()
    print(evaluator.euc_accuracy)
    print(evaluator.cos_accuracy)


if __name__ == "__main__":
    main()
