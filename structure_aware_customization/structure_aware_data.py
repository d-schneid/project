import ast
import json
from typing import Optional, List, TYPE_CHECKING

from data_handler import DataHandler, PAD_TOK_ID_DFG

from torch.utils.data import Dataset, DataLoader
import torch
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_sequence
from lightning.pytorch.utilities.types import EVAL_DATALOADERS, TRAIN_DATALOADERS

from nemo.collections.llm.gpt.data.mock import MockDataModule
from nemo.utils.import_utils import safe_import

_, HAVE_TE = safe_import("transformer_engine")

if TYPE_CHECKING:
	from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec

with open('../../data/pretraining/metadata.json', 'r') as f_metadata:
	metadata = json.load(f_metadata)

PAD_TOK_ID_AST = metadata['num_ast_node_types']


class StructureAwareDataModule(MockDataModule):

	def __init__(
			self,
			seq_length: int = 2048,
			tokenizer: Optional["TokenizerSpec"] = None,
			micro_batch_size: int = 2,
			global_batch_size: int = 16,
			rampup_batch_size: Optional[List[int]] = None,
			num_train_samples: int = 10_000,
			num_val_samples: int = 10_000,
			num_test_samples: int = 10_000,
			num_workers: int = 1,
			pin_memory: bool = True,
			persistent_workers: bool = False,
			create_attention_mask: bool = False,
			vocab_file: Optional[str] = None,
			merges_file: Optional[str] = None,
	):
		super().__init__(
			seq_length=seq_length,
			tokenizer=tokenizer,
			micro_batch_size=micro_batch_size,
			global_batch_size=global_batch_size,
			rampup_batch_size=rampup_batch_size,
			num_train_samples=num_train_samples,
			num_val_samples=num_val_samples,
			num_test_samples=num_test_samples,
			num_workers=num_workers,
			pin_memory=pin_memory,
			persistent_workers=persistent_workers,
			create_attention_mask=create_attention_mask,
			vocab_file=vocab_file,
			merges_file=merges_file,
		)

	def setup(self, stage: str = "") -> None:
		self._train_ds = StructureAwareDataset('../../data/pretraining')
		self._validation_ds = StructureAwareDataset('../../data/pretraining')
		self._test_ds = StructureAwareDataset('../../data/pretraining')

	def train_dataloader(self) -> TRAIN_DATALOADERS:
		if not hasattr(self, "_train_ds"):
			self.setup()
		return self._create_dataloader(self._train_ds)

	def val_dataloader(self) -> EVAL_DATALOADERS:
		if not hasattr(self, "_validation_ds"):
			self.setup()
		return self._create_dataloader(self._validation_ds)

	def test_dataloader(self) -> EVAL_DATALOADERS:
		if not hasattr(self, "_test_ds"):
			self.setup()
		return self._create_dataloader(self._test_ds)

	def _create_dataloader(self, dataset, **kwargs) -> DataLoader:
		return DataLoader(
			dataset,
			num_workers=self.num_workers,
			pin_memory=self.pin_memory,
			persistent_workers=self.persistent_workers,
			collate_fn=dataset.collate_fn,
			**kwargs,
		)


class StructureAwareDataset(Dataset):

	def __init__(self, data_dir) -> None:
		super().__init__()
		self.data_handler = DataHandler(save_dir=data_dir)
		self.padding_value = self.data_handler.tokenizer.eos_token_id
		self.data = self.data_handler.get_concat_stored_data()

		self.data['code_tokens'] = (self.data['code_tokens'].apply(lambda x: list(map(int, x.split(',')))).
									apply(lambda x: torch.tensor(x)))

		self.data['code_tokens_pos_ids'] = (self.data['code_tokens_pos_ids'].
											apply(lambda x: list(map(int, x.split(',')))).
											apply(lambda x: torch.tensor(x)))

		self.data['code_tokens_rel_pos_ids'] = (self.data['code_tokens_rel_pos_ids'].apply(ast.literal_eval).
												apply(lambda x: torch.tensor(x)))

		self.data['ll_sims'] = (self.data['ll_sims'].
								apply(lambda x: [list(map(float, sublist.split(','))) for sublist in x.split(';')]).
								apply(pad_inner_lists, padding_value=self.padding_value, padding_side='left'))

		self.data['lr_paths_types'] = (self.data['lr_paths_types'].apply(lambda x: ast.literal_eval(x)).
									   apply(pad_inner_lists, padding_value=PAD_TOK_ID_AST))

		self.data['lr_paths_len'] = (self.data['lr_paths_len'].apply(lambda x: list(map(int, x.split(',')))).
									 apply(lambda x: torch.tensor(x)))

		self.data['dfg_node_mask'] = (self.data['dfg_node_mask'].apply(lambda x: list(map(int, x.split(',')))).
									  apply(lambda x: torch.tensor(x)))

		self.data['attn_code_tokens'] = self.data['attn_code_tokens'].apply(ast.literal_eval).apply(lambda x: torch.tensor(x))

		self.data['attn_ast_leaves'] = self.data['attn_ast_leaves'].apply(ast.literal_eval).apply(lambda x: torch.tensor(x))

		self.data['attn_dfg_edges'] = self.data['attn_dfg_edges'].apply(ast.literal_eval).apply(lambda x: torch.tensor(x))

		self.data['attn_code_ast'] = self.data['attn_code_ast'].apply(ast.literal_eval).apply(lambda x: torch.tensor(x))

		self.data['attn_code_dfg'] = self.data['attn_code_dfg'].apply(ast.literal_eval).apply(lambda x: torch.tensor(x))

	def __len__(self) -> int:
		return len(self.data)

	def __getitem__(self, idx):
		code_tokens = self.data.iloc[idx]['code_tokens']

		# TODO: Check how to deal with offset of code_token_ids and labels, loss_mask
		# TODO: adapt attention mask
		batch = {
			'code_token_ids': code_tokens,
			'code_token_pos_ids': self.data.iloc[idx]['code_tokens_pos_ids'],
			'code_token_rel_pos_ids': self.data.iloc[idx]['code_tokens_rel_pos_ids'],
			'll_sims': self.data.iloc[idx]['ll_sims'],
			'lr_paths_types': self.data.iloc[idx]['lr_paths_types'],
			'lr_paths_len': self.data.iloc[idx]['lr_paths_len'],
			'dfg_node_mask': self.data.iloc[idx]['dfg_node_mask'],
			'attn_code_tokens': self.data.iloc[idx]['attn_code_tokens'],
			'attn_ast_leaves': self.data.iloc[idx]['attn_ast_leaves'],
			'attn_dfg_edges': self.data.iloc[idx]['attn_dfg_edges'],
			'attn_code_ast': self.data.iloc[idx]['attn_code_ast'],
			'attn_code_dfg': self.data.iloc[idx]['attn_code_dfg'],
			'labels': code_tokens,
			'loss_mask': torch.ones(len(code_tokens))
		}

		return batch

	def collate_fn(self, batch):
		# Initialize a dictionary to store the batch data
		batch_dict = {}
		for key in batch[0].keys():
			batch_dict[key] = [sample[key] for sample in batch]
			if key not in ['code_token_ids', 'code_token_pos_ids', 'dfg_node_mask', 'lr_paths_len', 'labels', 'loss_mask']:
				if key == 'lr_paths_types':
					batch_dict[key] = pad_2d_tensors(batch_dict[key], padding_value=PAD_TOK_ID_AST)
				else:
					batch_dict[key] = pad_2d_tensors(batch_dict[key], padding_value=self.padding_value)

			padding_value = self.padding_value if key != 'dfg_node_mask' else PAD_TOK_ID_DFG
			batch_dict[key] = pad_sequence(batch_dict[key], batch_first=True, padding_value=padding_value)

		batch_dict = pad_labels_loss_mask(batch_dict)

		# individual padded attention masks
		attn_code_tokens = batch_dict['attn_code_tokens']
		attn_ast_leaves = batch_dict['attn_ast_leaves']
		attn_dfg_edges = batch_dict['attn_dfg_edges']
		attn_code_ast = batch_dict['attn_code_ast']
		attn_code_dfg = batch_dict['attn_code_dfg']

		# Compute transpose
		attn_code_ast_T = attn_code_ast.transpose(1, 2)
		attn_code_dfg_T = attn_code_dfg.transpose(1, 2)

		# Compute null matrix for attention between AST leaves and DFG edges
		attn_ast_dfg = torch.zeros(attn_ast_leaves.size(0), attn_ast_leaves.size(1), attn_dfg_edges.size(2))
		attn_ast_dfg_T = attn_ast_dfg.transpose(1, 2)

		# Build block matrices column-wise
		first_col_matrix = torch.cat((attn_code_tokens, attn_code_ast_T, attn_code_dfg_T), dim=1)
		second_col_matrix = torch.cat((attn_code_ast, attn_ast_leaves, attn_ast_dfg_T), dim=1)
		third_col_matrix = torch.cat((attn_code_dfg, attn_ast_dfg, attn_dfg_edges), dim=1)
		attn_mask = torch.cat((first_col_matrix, second_col_matrix, third_col_matrix), dim=2)

		batch_dict['attention_mask'] = attn_mask.unsqueeze(1) # broadcast across all attention heads

		keys_to_remove = ['attn_code_tokens', 'attn_ast_leaves', 'attn_dfg_edges', 'attn_code_ast', 'attn_code_dfg']
		for key in keys_to_remove:
			del batch_dict[key]

		return batch_dict


def pad_labels_loss_mask(batch_dict):
	labels = batch_dict['labels']
	loss_mask = batch_dict['loss_mask']
	dfg_node_mask = batch_dict['dfg_node_mask']
	lr_paths_len = batch_dict['lr_paths_len']

	pad_len = dfg_node_mask[0].size(0) + lr_paths_len[0].size(0)

	padded_labels = []
	padded_loss_mask = []
	for label, mask in zip(labels, loss_mask):
		padded_label = F.pad(label, (0, pad_len), value=0)
		padded_mask = F.pad(mask, (0, pad_len), value=0)
		padded_labels.append(padded_label)
		padded_loss_mask.append(padded_mask)

	batch_dict['labels'] = torch.stack(padded_labels)
	batch_dict['loss_mask'] = torch.stack(padded_loss_mask)

	return batch_dict


def pad_inner_lists(list_of_lists, padding_value, padding_side='right'):
	tensors = [torch.tensor(x) for x in list_of_lists]

	return pad_sequence(tensors, batch_first=True, padding_value=padding_value, padding_side=padding_side) if tensors else [torch.tensor(-1)]


def pad_2d_tensors(tensor_list, padding_value, padding_side='right'):
	max_rows = max(tensor.size(0) for tensor in tensor_list)
	max_cols = max(tensor.size(1) for tensor in tensor_list)

	padded_tensors = []
	for tensor in tensor_list:
		rows_to_pad = max_rows - tensor.size(0)
		cols_to_pad = max_cols - tensor.size(1)

		if padding_side == 'right':
			padded_tensor = torch.nn.functional.pad(tensor, (0, cols_to_pad, 0, rows_to_pad), mode='constant', value=padding_value)
		else:
			padded_tensor = torch.nn.functional.pad(tensor, (cols_to_pad, 0, 0, rows_to_pad), mode='constant', value=padding_value)

		padded_tensors.append(padded_tensor)

	return padded_tensors
