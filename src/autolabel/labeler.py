from loguru import logger
from rich.progress import (
    track,
    Progress,
    BarColumn,
    MofNCompleteColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TextColumn,
)
from rich.console import Group
from rich.live import Live
from typing import Tuple, List, Dict, Union, Optional
import numpy as np
import pandas as pd
import sys

from autolabel.confidence import ConfidenceCalculator
from autolabel.cache import SQLAlchemyCache
from autolabel.few_shot import ExampleSelectorFactory
from autolabel.models import ModelFactory, BaseModel
from autolabel.schema import LLMAnnotation
from autolabel.tasks import TaskFactory
from autolabel.database import StateManager
from autolabel.schema import TaskRun, TaskStatus
from autolabel.data_models import TaskRunModel, AnnotationModel
from autolabel.configs import AutolabelConfig


class LabelingAgent:
    CHUNK_SIZE = 5
    COST_KEY = "Cost in $"

    def __init__(
        self,
        config: Union[str, Dict],
        log_level: Optional[str] = "INFO",
        cache: Optional[bool] = True,
    ) -> None:
        self.db = StateManager()
        logger.remove()
        logger.add(sys.stdout, level=log_level)
        self.cache = SQLAlchemyCache() if cache else None

        self.config = AutolabelConfig(config)
        self.task = TaskFactory.from_config(self.config)
        self.llm: BaseModel = ModelFactory.from_config(self.config, cache=self.cache)
        self.confidence = ConfidenceCalculator(
            score_type="logprob_average", llm=self.llm
        )

    # TODO: all this will move to a separate input parser class
    # this is a temporary solution to quickly add this feature and unblock expts
    def _read_csv(
        self,
        csv_file: str,
        config: AutolabelConfig,
        max_items: int = None,
        start_index: int = 0,
    ) -> Tuple[pd.DataFrame, List[Dict], List]:
        logger.debug(f"reading the csv from: {start_index}")
        delimiter = config.delimiter()
        label_column = config.label_column()

        dat = pd.read_csv(csv_file, sep=delimiter, dtype="str")[start_index:]
        if max_items and max_items > 0:
            max_items = min(max_items, len(dat))
            dat = dat[:max_items]

        inputs = dat.to_dict(orient="records")
        gt_labels = None if not label_column else dat[label_column].tolist()
        return (dat, inputs, gt_labels)

    def _read_dataframe(
        self,
        df: pd.DataFrame,
        config: AutolabelConfig,
        max_items: int = None,
        start_index: int = 0,
    ) -> Tuple[pd.DataFrame, List[Dict], List]:
        label_column = config.label_column()

        dat = df[start_index:]
        if max_items and max_items > 0:
            max_items = min(max_items, len(dat))
            dat = dat[:max_items]

        inputs = dat.to_dict(orient="records")
        gt_labels = None if not label_column else dat[label_column].tolist()
        return (dat, inputs, gt_labels)

    def run(
        self,
        dataset: Union[str, pd.DataFrame],
        max_items: Optional[int] = None,
        output_name: Optional[str] = None,
        start_index: Optional[int] = 0,
        eval_every: Optional[int] = 50,
    ) -> None:
        """Labels data in a given dataset. Output written to new CSV file.

        Args:
            dataset: path to CSV dataset to be annotated
            max_items: maximum items in dataset to be annotated
            output_name: custom name of output CSV file
            start_index: skips annotating [0, start_index)
        """

        self.db.initialize()
        self.dataset = self.db.initialize_dataset(
            dataset, self.config, start_index, max_items
        )
        self.task_object = self.db.initialize_task(self.config)
        csv_file_name = (
            output_name if output_name else f"{dataset.replace('.csv','')}_labeled.csv"
        )
        if isinstance(dataset, str):
            df, inputs, gt_labels = self._read_csv(
                dataset, self.config, max_items, start_index
            )
        elif isinstance(dataset, pd.DataFrame):
            df, inputs, gt_labels = self._read_dataframe(
                dataset, self.config, max_items, start_index
            )

        # Check explanations are present in data if explanation_column is passed in
        if (
            self.config.explanation_column()
            and self.config.explanation_column() not in df.keys().tolist()
        ):
            raise ValueError(
                f"Explanation column {self.config.explanation_column()} not found in dataset.\nMake sure that explanations were generated using labeler.generate_explanations(seed_file)."
            )

        # Initialize task run and check if it already exists
        self.task_run = self.db.get_task_run(self.task_object.id, self.dataset.id)
        # Resume/Delete the task if it already exists or create a new task run
        if self.task_run:
            logger.info("Task run already exists.")
            self.task_run = self.handle_existing_task_run(
                self.task_run, csv_file_name, gt_labels=gt_labels
            )
        else:
            self.task_run = self.db.create_task_run(
                csv_file_name, self.task_object.id, self.dataset.id
            )

        # Get the seed examples from the dataset config
        seed_examples = self.config.few_shot_example_set()

        # If this dataset config is a string, read the corrresponding csv file
        if isinstance(seed_examples, str):
            _, seed_examples, _ = self._read_csv(seed_examples, self.config)

        self.example_selector = ExampleSelectorFactory.initialize_selector(
            self.config, seed_examples, df.keys().tolist()
        )

        num_failures = 0
        current_index = self.task_run.current_index
        cost = 0.0
        postfix_dict = {}

        progress = Progress(
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        )

        postfix = Progress(
            TextColumn("{task.fields[postfix]}"),
        )

        group = Group(progress, postfix)
        live = Live(group)

        with live:
            indices = range(current_index, len(inputs), self.CHUNK_SIZE)
            progress_display = progress.add_task(
                "Generating Responses...", total=len(inputs)
            )
            postfix_display = postfix.add_task("Postfix", postfix="")
            for current_index in indices:
                chunk = inputs[current_index : current_index + self.CHUNK_SIZE]
                final_prompts = []
                for i, input_i in enumerate(chunk):
                    # Fetch few-shot seed examples
                    if self.example_selector:
                        examples = self.example_selector.select_examples(input_i)
                    else:
                        examples = []
                    # Construct Prompt to pass to LLM
                    final_prompt = self.task.construct_prompt(input_i, examples)
                    final_prompts.append(final_prompt)

                # Get response from LLM
                try:
                    response, curr_cost = self.llm.label(final_prompts)
                except Exception as e:
                    # TODO (dhruva): We need to handle this case carefully
                    # When we erorr out, we will have less elements in the llm_labels
                    # than the gt_labels array, with the 1:1 mapping not being
                    # maintained either. We should either remove the elements we errored
                    # out on from gt_labels or add None labels to the llm_labels.
                    logger.error(
                        "Error in generating response:" + repr(e), "Prompt: ", chunk
                    )
                    for i in range(len(chunk)):
                        annotation = LLMAnnotation(
                            successfully_labeled="No",
                            label=self.task.NULL_LABEL_TOKEN,
                            raw_response="",
                            curr_sample=chunk[i],
                            prompt=final_prompts[i],
                            confidence_score=0,
                        )
                        AnnotationModel.create_from_llm_annotation(
                            self.db.session,
                            annotation,
                            current_index + i,
                            self.task_run.id,
                        )
                    num_failures += len(chunk)
                    response = None

                if response is not None:
                    for i in range(len(response.generations)):
                        response_item = response.generations[i]
                        annotations = []
                        for generation in response_item:
                            if self.config.confidence():
                                annotation = self.confidence.calculate(
                                    model_generation=self.task.parse_llm_response(
                                        generation, chunk[i], final_prompts[i]
                                    ),
                                    prompt=final_prompts[i],
                                )
                            else:
                                annotation = self.task.parse_llm_response(
                                    generation, chunk[i], final_prompts[i]
                                )
                            annotations.append(annotation)
                        final_annotation = self.majority_annotation(annotations)
                        AnnotationModel.create_from_llm_annotation(
                            self.db.session,
                            final_annotation,
                            current_index + i,
                            self.task_run.id,
                        )
                cost += curr_cost
                postfix_dict[self.COST_KEY] = f"{cost:.2f}"

                # Evaluate the task every eval_every examples
                if (current_index + self.CHUNK_SIZE) % eval_every == 0:
                    db_result = AnnotationModel.get_annotations_by_task_run_id(
                        self.db.session, self.task_run.id
                    )
                    llm_labels = [LLMAnnotation(**a.llm_annotation) for a in db_result]
                    if gt_labels:
                        eval_result = self.task.eval(
                            llm_labels, gt_labels[: len(llm_labels)]
                        )

                        for m in eval_result:
                            if not isinstance(m.value, list) or len(m.value) < 1:
                                continue
                            elif isinstance(m.value[0], float):
                                postfix_dict[m.name] = f"{m.value[0]:.4f}"
                            elif len(m.value[0]) > 0:
                                postfix_dict[m.name] = f"{m.value[0][0]:.4f}"

                progress.update(
                    progress_display,
                    advance=self.CHUNK_SIZE,
                )
                postfix.update(
                    postfix_display,
                    postfix=", ".join([f"{k}={v}" for k, v in postfix_dict.items()]),
                )

                # Update task run state
                self.task_run = self.save_task_run_state(
                    current_index=current_index + len(chunk)
                )

        db_result = AnnotationModel.get_annotations_by_task_run_id(
            self.db.session, self.task_run.id
        )
        llm_labels = [LLMAnnotation(**a.llm_annotation) for a in db_result]
        eval_result = None
        # if true labels are provided, evaluate accuracy of predictions
        if gt_labels:
            eval_result = self.task.eval(llm_labels, gt_labels[: len(llm_labels)])
            # TODO: serialize and write to file
            for m in eval_result:
                print(f"Metric: {m.name}: {m.value}")

        # Write output to CSV
        output_df = df.copy()
        output_df[self.config.task_name() + "_llm_labeled_successfully"] = [
            l.successfully_labeled for l in llm_labels
        ]
        output_df[self.config.task_name() + "_llm_label"] = [
            l.label for l in llm_labels
        ]
        if self.config.confidence():
            output_df["llm_confidence"] = [l.confidence_score for l in llm_labels]

        # Only save to csv if output_name is provided or dataset is a string
        if output_name:
            csv_file_name = output_name
        elif isinstance(dataset, str):
            csv_file_name = f"{dataset.replace('.csv','')}_labeled.csv"
            output_df.to_csv(
                csv_file_name,
                sep=self.config.delimiter(),
                header=True,
                index=False,
            )

        print(f"Total number of failures: {num_failures}")
        return (
            output_df[self.config.task_name() + "_llm_label"],
            output_df,
            eval_result,
        )

    def plan(
        self,
        dataset: Union[str, pd.DataFrame],
        max_items: int = None,
        start_index: int = 0,
    ):
        """Calculates and prints the cost of calling autolabel.run() on a given dataset

        Args:
            dataset: path to a CSV dataset
        """

        if isinstance(dataset, str):
            df, inputs, _ = self._read_csv(dataset, self.config, max_items, start_index)
        elif isinstance(dataset, pd.DataFrame):
            df, inputs, _ = self._read_dataframe(
                dataset, self.config, max_items, start_index
            )

        # Check explanations are present in data if explanation_column is passed in
        if (
            self.config.explanation_column()
            and self.config.explanation_column() not in df.keys().tolist()
        ):
            raise ValueError(
                f"Explanation column {self.config.explanation_column()} not found in dataset.\nMake sure that explanations were generated using labeler.generate_explanations(seed_file)."
            )

        prompt_list = []
        total_cost = 0

        # Get the seed examples from the dataset config
        seed_examples = self.config.few_shot_example_set()

        # If this dataset config is a string, read the corrresponding csv file
        if isinstance(seed_examples, str):
            _, seed_examples, _ = self._read_csv(seed_examples, self.config)

        self.example_selector = ExampleSelectorFactory.initialize_selector(
            self.config, seed_examples, df.keys().tolist()
        )

        input_limit = min(len(inputs), 100)
        num_sections = max(input_limit / self.CHUNK_SIZE, 1)
        for chunk in track(
            np.array_split(inputs[:input_limit], num_sections), description=""
        ):
            for i, input_i in enumerate(chunk):
                # TODO: Check if this needs to use the example selector
                if self.example_selector:
                    examples = self.example_selector.select_examples(input_i)
                else:
                    examples = []
                final_prompt = self.task.construct_prompt(input_i, examples)
                prompt_list.append(final_prompt)

                # Calculate the number of tokens
                curr_cost = self.llm.get_cost(prompt=final_prompt, label="")
                total_cost += curr_cost

        total_cost = total_cost * (len(inputs) / input_limit)
        print(f"Total Estimated Cost: ${round(total_cost, 3)}")
        print(f"Number of examples to label: {len(inputs)}")
        print(f"Average cost per example: ${round(total_cost/len(inputs), 5)}")
        print(f"\n\nA prompt example:\n\n{prompt_list[0]}")
        return

    def handle_existing_task_run(
        self, task_run: TaskRun, csv_file_name: str, gt_labels: List[str] = None
    ) -> TaskRun:
        print(f"There is an existing task with following details: {task_run}")
        db_result = AnnotationModel.get_annotations_by_task_run_id(
            self.db.session, task_run.id
        )
        llm_labels = [LLMAnnotation(**a.llm_annotation) for a in db_result]
        if gt_labels and len(llm_labels) > 0:
            print("Evaluating the existing task...")
            gt_labels = gt_labels[: len(llm_labels)]
            eval_result = self.task.eval(llm_labels, gt_labels)
            for m in eval_result:
                print(f"Metric: {m.name}: {m.value}")
        print(f"{len(llm_labels)} examples have been labeled so far.")
        if len(llm_labels) > 0:
            print(f"Last annotated example - Prompt: {llm_labels[-1].prompt}")
            print(f"Annotation: {llm_labels[-1].label}")

        resume = None
        while resume is None:
            user_input = input("Do you want to resume the task? (y/n)")
            if user_input.lower() in ["y", "yes"]:
                print("Resuming the task...")
                resume = True
            elif user_input.lower() in ["n", "no"]:
                resume = False

        if not resume:
            TaskRunModel.delete_by_id(self.db.session, task_run.id)
            print("Deleted the existing task and starting a new one...")
            task_run = self.db.create_task_run(
                csv_file_name, self.task_object.id, self.dataset.id
            )
        return task_run

    def save_task_run_state(
        self, current_index: int = None, status: TaskStatus = "", error: str = ""
    ):
        # Save the current state of the task
        if error:
            self.task_run.error = error
        if status:
            self.task_run.status = status
        if current_index:
            self.task_run.current_index = current_index
        return TaskRunModel.update(self.db.session, self.task_run)

    def majority_annotation(
        self, annotation_list: List[LLMAnnotation]
    ) -> LLMAnnotation:
        labels = [a.label for a in annotation_list]
        counts = {}
        for ind, label in enumerate(labels):
            # Needed for named entity recognition which outputs lists instead of strings
            label = str(label)

            if label not in counts:
                counts[label] = (1, ind)
            else:
                counts[label] = (counts[label][0] + 1, counts[label][1])
        max_label = max(counts, key=lambda x: counts[x][0])
        return annotation_list[counts[max_label][1]]

    def generate_explanations(
        self,
        seed_examples: Union[str, List[Dict]],
    ) -> List[Dict]:
        out_file = None
        if isinstance(seed_examples, str):
            out_file = seed_examples
            _, seed_examples, _ = self._read_csv(seed_examples, self.config)

        explanation_column = self.config.explanation_column()
        if not explanation_column:
            raise ValueError(
                "The explanation column needs to be specified in the dataset config."
            )

        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
        ) as progress:
            task = progress.add_task(
                "Generating explanations...", total=len(seed_examples)
            )
            for seed_example in seed_examples:
                explanation_prompt = self.task.get_explanation_prompt(seed_example)
                explanation, _ = self.llm.label([explanation_prompt])
                explanation = explanation.generations[0][0].text
                seed_example["explanation"] = str(explanation) if explanation else ""
                progress.advance(task)

        if out_file:
            df = pd.DataFrame.from_records(seed_examples)
            df.to_csv(out_file, index=False)

        return seed_examples