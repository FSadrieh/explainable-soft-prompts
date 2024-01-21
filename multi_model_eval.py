import argparse
from simple_parsing import parse_known_args
from transformers import AutoTokenizer
from lightning import Trainer, seed_everything
import csv


from src.evaluation.utils import get_model_names_from_numbers
from src.training.model import BasicLM
from src.training.data_loading import LMDataModule
from args import TrainingArgs


def arg_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("soft_prompt_name", type=str)
    parser.add_argument("model_numbers", type=str)
    parser.add_argument("config", type=str, help="path to the config file for the validation")
    parser.add_argument("-a", "--accelerator", type=str, default="cuda", help="Supports: cuda, cpu, tpu, mps")
    parser.add_argument("-p", "--prompt_length", type=int, default=16)
    return parser.parse_args()


def main():
    seed_everything(workers=True, seed=42)

    args = arg_parser()
    # We want to validate the modle on each of the 25 models
    model_names = get_model_names_from_numbers(range(25))
    tokenizer = AutoTokenizer.from_pretrained(model_names[0], use_fast=True)
    val_losses = []
    for model_name in model_names:
        training_args, __ = parse_known_args(TrainingArgs, config_path=args.config)
        model_args = dict(
            model_names_or_paths=[model_name],
            tokenizer=tokenizer,
            from_scratch=training_args.from_scratch,
            learning_rate=training_args.learning_rate,
            weight_decay=training_args.weight_decay,
            beta1=training_args.beta1,
            beta2=training_args.beta2,
            lr_schedule=training_args.lr_schedule,
            warmup_period=training_args.warmup_period,
            prompt_length=training_args.prompt_length,
            init_text=training_args.init_text,
            init_embedding_models=training_args.init_embedding_models,
            init_embedding_mode=training_args.init_embedding_mode,
            init_seed=training_args.init_seed,
            local_soft_prompt=f"logs/explainable-soft-prompts/{args.soft_prompt_name}/checkpoints/soft_prompt.pt",
        )
        model = BasicLM(**model_args)

        dm = LMDataModule(training_args=training_args, tokenizer=tokenizer, prompt_length=args.prompt_length)

        trainer = Trainer(
            max_epochs=training_args.training_goal,
            devices=training_args.num_devices,
            accelerator=args.accelerator,
            strategy=training_args.distributed_strategy,
            deterministic=training_args.force_deterministic,
            precision=training_args.precision,
            gradient_clip_val=training_args.grad_clip,
            inference_mode=not training_args.compile,  # inference_mode for val/test and PyTorch 2.0 compiler don't like each other
        )

        print(f"Validating {args.soft_prompt_name} on {model_name}")
        val_losses.append(trainer.validate(model, dm)[0]["val/loss"])

    with open(f"logs/explainable-soft-prompts/{args.soft_prompt_name}/checkpoints/val_losses.csv", "w+") as f:
        writer = csv.writer(f)
        writer.writerow(["seed", "val_loss", "trained_on"])
        for i, val_loss in enumerate(val_losses):
            writer.writerow([model_names[i], val_loss, "Yes" if i in args.model_numbers.split(",") else "No"])


if __name__ == "__main__":
    main()