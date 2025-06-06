import os
import pickle
import re
import tokenize
import ast
from io import StringIO
from types import SimpleNamespace

import numpy as np
from tqdm import tqdm
from transformers import AutoTokenizer
import pandas as pd

from attn_mask import AttnMask
from code_completion_attn_mask import CodeCompletionAttnMask

tqdm.pandas()
from datasets import load_dataset

START_TOK_ID_DFG = 0
PAD_TOK_ID_DFG = 2


class DataHandler:

	def __init__(self, save_dir, dataset='code_search_net', lang='python',
				 tokenizer=AutoTokenizer.from_pretrained('bigcode/starcoder2-3b'), attn_mask_builder: AttnMask=CodeCompletionAttnMask()):
		self.save_dir = save_dir
		self.dataset = dataset
		self.lang = lang
		self.tokenizer = tokenizer
		self.attn_mask_builder = attn_mask_builder

	def read_dataset(self, split, max_samples=None):
		np.random.seed(10)
		dataset = load_dataset(self.dataset, self.lang)
		rows = []

		num_samples_in_split = len(dataset[split])
		indices = list(range(num_samples_in_split))
		if (max_samples is not None) and (num_samples_in_split > max_samples):
			indices = list(map(int, np.random.choice(indices, max_samples, replace=False)))
		pbar = tqdm(indices)
		pbar.set_description('Reading split=' + split)

		for i in pbar:
			sample = dataset[split][i]
			rows.append([sample['func_documentation_string'], sample['func_code_string']])

		return pd.DataFrame(rows, columns=['text', 'code'])

	def get_tokenizer_chars(self):
		tokenizer_chars = []

		for i in range(self.tokenizer.vocab_size):
			token = self.tokenizer.decode(i)
			if len(token) == 1:
				tokenizer_chars.append(token)

		tokenizer_chars = [c for c in tokenizer_chars if c != '�']

		return tokenizer_chars

	def remove_comments_and_docstrings(self, source):
		"""
		Returns 'source' minus comments and docstrings.
		"""
		io_obj = StringIO(source)
		out = ""
		prev_toktype = tokenize.INDENT
		last_lineno = -1
		last_col = 0

		for tok in tokenize.generate_tokens(io_obj.readline):
			token_type = tok[0]
			token_string = tok[1]
			start_line, start_col = tok[2]
			end_line, end_col = tok[3]
			line_text = tok[4]

			if start_line > last_lineno:
				last_col = 0 # start at beginning of new line
			if start_col > last_col:
				out += (" " * (start_col - last_col)) # add space between tokens

			# Remove comments:
			if token_type == tokenize.COMMENT:
				pass
			# This series of conditionals removes docstrings:
			elif token_type == tokenize.STRING:
				if prev_toktype != tokenize.INDENT:
					# This is likely a docstring; double-check we're not inside an operator:
					if prev_toktype != tokenize.NEWLINE:
						if start_col > 0:
							out += token_string
			else:
				out += token_string

			prev_toktype = token_type
			last_col = end_col
			last_lineno = end_line

		temp = []
		for row in out.split('\n'):
			if row.strip() != "":
				temp.append(row)
		code = '\n'.join(temp)

		pos = 0
		docstring_quotes = '"""'
		while pos < len(code):
			try:
				start = code[pos:].index(docstring_quotes) + pos
				end = code[start + len(docstring_quotes):].index(docstring_quotes) + start + len(docstring_quotes)
				code = code[:start] + code[end + len(docstring_quotes):]
				pos = start
			except:
				break

		return re.sub(r"\r\n\s*\r\n", '\n', code)

	def preprocess(self, data):
		failed_count = 0
		rows = []
		tokenizer_chars = self.get_tokenizer_chars()
		pbar = tqdm(data.itertuples())

		for row in pbar:
			code = row.code.strip().replace('▁', '_').replace('\r\n', '\n')  # step 1
			code = ''.join(filter(lambda c: c in tokenizer_chars, code))  # step 2
			try:
				code = self.remove_comments_and_docstrings(code)  # step 3
			except:
				failed_count += 1
				pbar.set_description('failed_count=' + str(failed_count))
				continue

			rows.append([row.text.strip(), code])

		data = pd.DataFrame(rows, columns=['text', 'code'])

		return data

	def convert_tokens_to_strings(self, data):
		data = data.drop(columns=['ast_leaf_tokens', 'ast_leaf_ranges', 'code_tokens_ranges'])
		for col in ['code_tokens', 'text_tokens']:
			data[col] = data[col].progress_apply(lambda l: ','.join(list(map(str, l))))

		return data.sample(frac=1).reset_index(drop=True)

	def get_lr_path(self, leaf):
		path = [leaf]
		while path[-1].parent is not None:
			path.append(path[-1].parent)

		return path

	def clean_data(self, data):
		return data[data['dfg_edges'].apply(lambda row: row != [])].reset_index(drop=True)

	def get_ll_sim(self, lr_path1, lr_path2):
		node_types = [node.type for node in lr_path1 + lr_path2]
		if '<START_AST>' in node_types or '<END_AST>' in node_types: return 0
		common = 1 # root is always common
		for i in range(2, min(len(lr_path1), len(lr_path2)) + 1):
			if lr_path1[-i] == lr_path2[-i]:
				common += 1
			else:
				break

		return common * common / (len(lr_path1) * len(lr_path2))

	def add_ast_lr_paths_and_ll_sim(self, data):
		ll_sims = []
		lr_paths = []
		all_node_types = set()

		for i, row in tqdm(enumerate(data.itertuples())):
			curr_lr_paths = [[SimpleNamespace(type='<START_AST>')]] + [self.get_lr_path(leaf) for leaf in row.ast_leaves] + [[SimpleNamespace(type='<END_AST>')]]
			num_ast_leaves = min(len(curr_lr_paths), 512)
			curr_ll_sims = np.ones((num_ast_leaves, num_ast_leaves))

			for i in range(num_ast_leaves - 1):
				for j in range(i + 1, num_ast_leaves):
					curr_ll_sims[i, j] = curr_ll_sims[j, i] = self.get_ll_sim(curr_lr_paths[i], curr_lr_paths[j])

			ll_sims.append(';'.join([','.join(list(map(str, row))) for row in curr_ll_sims]))
			lr_paths.append([[node.type for node in path] for path in curr_lr_paths])
			all_node_types.update(set(np.concatenate(lr_paths[-1])))

		data.drop(columns=['ast_leaves'], inplace=True)
		data['ll_sims'] = ll_sims
		data['lr_paths_types'] = lr_paths
		data['lr_paths_len'] = data['lr_paths_types'].apply(lambda row: ",".join(str(len(sublist)) for sublist in row))

		return all_node_types

	def map_dfg_node_code_token_idices(self, data):
		""""
		A DFG node/variable can correspond to multiple code tokens due to tokenization.
		This function maps each DFG node/variable to the corresponding code tokens.
		"""
		dfg_node_code_token_idxs = []
		dfg_edges = []

		for row in tqdm(data.itertuples()):
			if len(row.dfg_edges) > 0:
				dfg_nodes = sorted(list(set(np.concatenate([[left] + right for left, right in row.dfg_edges]))))
			else:
				dfg_nodes = []

			dfg_node_to_idx = {k: i for i, k in enumerate(dfg_nodes)}
			# DFG was built with the indices of AST leaves
			# Thus, the index of a DFG node can be used to retrieve the corresponding AST leaf and its code tokens
			dfg_node_code_token_idxs.append([row.ast_leaf_code_token_idxs[i] for i in dfg_nodes])
			dfg_edges.append([(dfg_node_to_idx[left], [dfg_node_to_idx[r] for r in right]) for left, right in row.dfg_edges])

		data['dfg_edges'] = dfg_edges
		data['dfg_node_code_token_idxs'] = dfg_node_code_token_idxs
		data['dfg_node_mask'] = [str(START_TOK_ID_DFG) + ","
								 + ",".join(["1" for _ in sublist])
								 + "," + str(PAD_TOK_ID_DFG) for sublist in dfg_node_code_token_idxs]

	def store_preprocessed_data(self, data, num_rows_per_file):
		# do memory intensive part in chunks
		os.makedirs(self.save_dir, exist_ok=True)
		all_node_types = set()
		global_max_rel_pos = 0

		for start in range(0, len(data), num_rows_per_file):
			chunk_data = data.iloc[start:start + num_rows_per_file].copy()  # copy so that edits are not on data
			chunk_node_types = self.add_ast_lr_paths_and_ll_sim(chunk_data)
			all_node_types.update(chunk_node_types)
			self.map_dfg_node_code_token_idices(chunk_data)
			self.add_special_tokens(chunk_data)
			chunk_data = self.attn_mask_builder.compute_attention_masks(chunk_data)
			chunk_data['code_tokens_rel_pos_ids'] = chunk_data['code_tokens_pos_ids'].apply(self.compute_relative_distances)
			chunk_data['text_tokens_rel_pos_ids'] = chunk_data['text_tokens_pos_ids'].apply(self.compute_relative_distances)
			chunk_max_rel_pos = max([row[0][-1] for row in chunk_data['code_tokens_rel_pos_ids']])
			global_max_rel_pos = max(global_max_rel_pos, chunk_max_rel_pos)

			cols = (['code_tokens', 'code_tokens_rel_pos_ids', 'lr_paths_types', 'lr_paths_len', 'll_sims',
					 'dfg_node_mask',]
					+ self.attn_mask_builder.get_cols())
			chunk_data = chunk_data[cols]

			for col in ['code_tokens_rel_pos_ids', 'lr_paths_types'] + self.attn_mask_builder.get_cols():
				chunk_data[col] = chunk_data[col].apply(str)

			chunk_data.to_parquet(os.path.join(self.save_dir, 'from_' + str(start) + '.parquet'), engine='fastparquet', row_group_offsets=100)

		return all_node_types, global_max_rel_pos

	def parse_list_of_lists(self, s, type_=int):
		list_of_lists = s[1:-2].split('], ')
		if type_ == str:
			list_of_lists = [[t[1:-1].replace('\\n', '\n').replace('\\\\', '\\') for t in x[1:].split(', ')] for x in list_of_lists]
		elif type_ == int:
			list_of_lists = [[int(t) for t in x[1:].split(', ')] for x in list_of_lists]
		else:
			raise Exception('Unknown value for type_')
		return list_of_lists

	def convert_node_types_to_indices(self, all_node_types):
		all_node_types = sorted(list(all_node_types))
		node_type_to_idx = {t: i for i, t in enumerate(all_node_types)}
		with open(os.path.join(self.save_dir, 'all_node_types.pkl'), 'wb') as f:
			pickle.dump(all_node_types, f)

		global_max_ast_depth = -1
		for filename in tqdm(os.listdir(self.save_dir)):
			if filename.startswith('from_'):
				chunk_data = pd.read_parquet(os.path.join(self.save_dir, filename), engine='fastparquet')
				chunk_data['lr_paths_types'] = chunk_data['lr_paths_types'].apply(lambda lr_path_types: str([[node_type_to_idx[node_type] for node_type in lr_path]
																											 for lr_path in self.parse_list_of_lists(lr_path_types, type_=str)]))
				chunk_data.to_parquet(os.path.join(self.save_dir, filename), engine='fastparquet', row_group_offsets=100)

				local_max_ast_depth = chunk_data['lr_paths_types'].apply(lambda x: ast.literal_eval(x)).apply(lambda row: max([len(sublist) for sublist in row])).max()
				if local_max_ast_depth > global_max_ast_depth: global_max_ast_depth = local_max_ast_depth

		return global_max_ast_depth

	def upper_triangle(self, ll_sims):
		rows = ll_sims.split(';')[:-1]
		ll_sims = ''
		for i, row in enumerate(rows):
			ll_sims += ','.join(row.split(',')[i + 1:]) + ';'
		return ll_sims[:-1]

	def reduce_ll_sims(self):
		# Reduce memory taken by ll_sims column by storing only upper triangles w/o diagonals
		pbar = tqdm(os.listdir(self.save_dir))
		for filename in pbar:
			pbar.set_description(filename)
			if filename.startswith('from_'):
				chunk_data = pd.read_parquet(os.path.join(self.save_dir, filename), engine='fastparquet')
				chunk_data['ll_sims'] = chunk_data['ll_sims'].apply(self.upper_triangle)
				chunk_data.to_parquet(os.path.join(self.save_dir, filename), engine='fastparquet', row_group_offsets=100)

	def get_concat_stored_data(self, split='train'):
		data = []
		data_dir = os.path.join(self.save_dir, split)
		for filename in tqdm(os.listdir(data_dir)):
			if filename.startswith('from_'):
				chunk_data = pd.read_parquet(os.path.join(data_dir, filename), engine='fastparquet')
				data.append(chunk_data)

		return pd.concat(data)

	def compute_relative_distances(self, pos_ids_str, max_distance=127):
		pos_ids = list(map(int, pos_ids_str.split(',')))
		distances = []
		for i in range(len(pos_ids)):
			row = []
			for j in range(len(pos_ids)):
				# account for padding distance id of 0 that is added when padded in collating a batch
				# thus, 0 should not be assigned as a relative distance
				raw_distance = abs(pos_ids[i] - pos_ids[j]) + 1
				distance = min(raw_distance, max_distance)
				row.append(distance)

			distances.append(row)

		return distances

	def add_special_tokens(self, data):
		data['code_tokens'] = data['code_tokens'].apply(lambda x: str(self.tokenizer.bos_token_id) + ',' + x + ',' + str(self.tokenizer.eos_token_id))
		data['code_tokens_pos_ids'] = data['code_tokens'].apply(lambda x: ','.join(map(str, range(len(x.split(','))))))

		data['text_tokens'] = data['text_tokens'].apply(lambda x: str(self.tokenizer.bos_token_id) + ',' + x + ',' + str(self.tokenizer.eos_token_id))
		data['text_tokens_pos_ids'] = data['text_tokens'].apply(lambda x: ','.join(map(str, range(len(x.split(','))))))

		# account for BOS token
		data['ast_leaf_code_token_idxs'] = data['ast_leaf_code_token_idxs'].apply(lambda x: [[x + 1 for x in sublist] for sublist in x])
		data['dfg_node_code_token_idxs'] = data['dfg_node_code_token_idxs'].apply(lambda x: [[x + 1 for x in sublist] for sublist in x])

		# account for padding of BOS and EOS tokens for DFG sequence
		data['dfg_edges'] = data['dfg_edges'].apply(lambda row: [(x + 1, [y + 1 for y in ys]) for x, ys in row])
