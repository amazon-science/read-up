# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: CC-BY-NC-4.0
import os
import torch
import logging
import jsonlines
import numpy as np
from tqdm import tqdm
import _pickle as cPickle
from functools import partial
from torch.utils.data import Dataset
from torch.nn.utils.rnn import pad_sequence
from src.tools.utils import Game, bbox2spatial_vilbert
from src.data.dataset import GuessWhatDataset


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class OracleDataset(GuessWhatDataset):
    def __init__(
        self,
        dataroot,
        split,
        image_features_reader,
        tokenizer,
        image_features_reader_gt,
        **kwargs
    ):
        super().__init__(
            dataroot,
            'oracle',
            split,
            image_features_reader,
            tokenizer,
            image_features_reader_gt,
            **kwargs)

    def _load_dataset(self):
        with jsonlines.open(self.data_path) as reader:
            # Build an index which maps image id with a list of qa annotations.
            entries = []
            for cur, annotation in tqdm(enumerate(reader)):
                # if cur >= 1000: break
                game = Game.from_annotation(annotation)
                if game.status != 'success':
                    continue
                for qa in game.qas:
                    item = dict()
                    item['game'] = game
                    item['image_id'] = game.image_id
                    item['target_index'] = game.target_index
                    item['target_bbox'] = bbox2spatial_vilbert(
                        game.bboxs[game.target_index], game.image_width, game.image_height, mode='xywh')
                    # feats, _, _ = self._image_features_reader[game.image_id]
                    # item['target_image_feature'] = feats[game.target_index]
                    item['target_category'] = game.categories[game.target_index]
                    item['question'] = qa['question']
                    q_tokens = self._tokenizer.encode(qa['question'])

                    assert q_tokens[-1] == self.eoq_id, "Game %d has question which is not ended with `?`." % game.id
                    item['q_tokens'] = [self._tokenizer.cls_id] + q_tokens
                    item['answer'] = [int(self.answer2id[qa['answer']])]
                    entries.append(item)
        return entries

    def tensorize(self):
        for entry in self.entries:
            entry['target_category'] = torch.from_numpy(np.array(entry['target_category']))
            entry['target_bbox'] = torch.from_numpy(np.array(entry['target_bbox']))
            # entry['target_image_feature'] = torch.from_numpy(np.array(entry['target_image_feature']))
            entry['q_tokens'] = torch.from_numpy(np.array(entry['q_tokens']))
            entry['answer'] = torch.from_numpy(np.array(entry['answer']))
            entry['target_index'] = torch.from_numpy(np.array(entry['target_index']))

    def __getitem__(self, index):
        entry = self.entries[index]
        game = entry['game']
        q_tokens = entry['q_tokens']
        tgt_cat = entry['target_category']
        tgt_bbox = entry['target_bbox']

        img_id = entry['image_id']
        tgt_idx = entry['target_index']
        feats, _, _ = self._image_features_reader_gt[img_id]
        tgt_img_feat = torch.from_numpy(np.array(feats[tgt_idx]))

        feats, _, bboxs, _ = self._image_features_reader[img_id]
        bg_img_feats = torch.from_numpy(np.array(feats))
        bg_bboxs = torch.from_numpy(np.array(bboxs))
        answer = torch.LongTensor(entry['answer'])

        return (
            game,
            tgt_cat,
            tgt_bbox,
            tgt_img_feat,
            bg_bboxs,
            bg_img_feats,
            q_tokens,
            answer
        )


def collate_fn(batch, wrd_pad_id):
    batch_size = len(batch)
    # batch
    game, tgt_cat, tgt_bbox, tgt_img_feat, bg_bboxs, bg_img_feats, q_tokens, answer = zip(*batch)

    q_len = [len(q) for q in q_tokens]
    max_q_len = max(q_len)
    txt_attn_mask = [[1] * len(q) + [0] * (max_q_len - len(q)) for q in q_tokens]
    txt_attn_mask = torch.from_numpy(np.array(txt_attn_mask))
    q_len = torch.LongTensor(q_len)

    tgt_cat = torch.stack(tgt_cat).long()
    tgt_bbox = torch.stack(tgt_bbox).float()
    tgt_img_feat = torch.stack(tgt_img_feat).float()
    bg_bboxs = torch.stack(bg_bboxs).float()
    bg_img_feats = torch.stack(bg_img_feats).float()
    # (batch_size, padded_seq_length)
    q_tokens = pad_sequence(q_tokens, batch_first=True, padding_value=wrd_pad_id).long()
    # (batch_size)
    answer = torch.stack(answer).view(batch_size).long()

    return game, tgt_cat, tgt_bbox, tgt_img_feat, bg_bboxs, bg_img_feats, q_tokens, q_len, txt_attn_mask, answer

