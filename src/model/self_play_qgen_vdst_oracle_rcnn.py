# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: CC-BY-NC-4.0
import torch
import torch.nn as nn
from src.model.qgen_vdst import QGenModel
from src.model.oracle_rcnn import OracleModel
from src.model.guesser import GuesserModel
from torch.nn.utils.rnn import pad_sequence

class SelfPlayModel(nn.Module):
    def __init__(
        self, 
        qgen_kwargs, 
        oracle_kwargs,
        guesser_kwargs,
    ):
        super(SelfPlayModel, self).__init__()
        self.qgen = None
        if qgen_kwargs is not None:
            self.qgen = QGenModel(**qgen_kwargs)
        self.oracle = OracleModel(**oracle_kwargs)
        self.guesser = GuesserModel(**guesser_kwargs)

    def load_player(self, player, path, map_location="cpu"):
        """
        Usage: 
            self_play_obj.load_play("guesser", ckpt_path)
        """
        assert player in ['qgen', 'oracle', 'guesser'],\
            "`player` should be one of ('qgen', 'oracle', 'guesser')."  
        getattr(self, player).load_state_dict(
            torch.load(path, map_location=map_location)['model']
        )
        return "Load %s from %s" % (player, path)

    # generate_sentence(
    #     self, last_wrd, obj_feats, eoq_token, eod_token, end_of_dialog, 
    #     max_q_len, pi=None, last_state=None, greedy=True):
    # return:
    #     q_tokens, actual_length, last_state, obj_repr, end_of_dialog

    def play(self, obj_feats, tgt_cat, tgt_bbox, tgt_img_feat, cats, bboxs, bboxs_mask, 
             sos_token, pad_token, eoq_token, eod_token, 
             answer2id, answer2token, max_q_len, greedy=True, max_turns=8):
        device = obj_feats.device
        batch_size = obj_feats.size(0)
        num_bboxs = obj_feats.size(1)
        end_of_dialog = torch.zeros(batch_size).bool().to(device)
        last_wrd = torch.zeros(batch_size).fill_(sos_token).long().to(device)
        last_state = None
        pi = (torch.ones(batch_size, num_bboxs) / num_bboxs).to(device)

        dialog = [torch.LongTensor(0).to(device) for _ in range(batch_size)]
        q_log = [[] for _ in range(batch_size)]
        a_log = [[] for _ in range(batch_size)]
        a_conf_log = [[] for _ in range(batch_size)]
        for turn in range(max_turns):
            q, q_len, state, obj_repr, end_of_dialog_next = self.qgen.generate_sentence(
                last_wrd, obj_feats, eoq_token, eod_token, end_of_dialog, 
                max_q_len=max_q_len, pi=pi, last_state=last_state, greedy=greedy
            )
            # q, state, q_len, end_of_dialog_next = self.qgen.generate(
            #     last_wrd, img_feat, eoq_token, eod_token, end_of_dialog, 
            #     max_q_len, last_state=last_state, greedy=greedy)
            
            pad_q = pad_sequence(q, batch_first=True, padding_value=pad_token)
            # HACK: length == 0 can not forward in RNN
            fake_q_len = q_len.clone()
            fake_q_len[q_len == 0] = 1
            a = self.oracle(pad_q, tgt_cat, tgt_bbox, tgt_img_feat, fake_q_len)
            a_confidence = nn.functional.softmax(a, dim=-1)
            a_idx = a.argmax(dim=-1)
            a = oracle_output_to_answer_token(a_idx, answer2id, answer2token)
            for b in range(batch_size):
                if not end_of_dialog[b]:
                    _q = q[b][:q_len[b]]
                    q_log[b].append(_q)
                    dialog[b] = torch.cat([dialog[b], _q])
                if not end_of_dialog_next[b]:
                    _a = a[b].view(-1)
                    a_log[b].append(_a)
                    a_conf_log[b].append(a_confidence[b, a_idx[b]])
                    dialog[b] = torch.cat([dialog[b], _a])

            if end_of_dialog_next.sum().item() == batch_size:
                break
            end_of_dialog = end_of_dialog_next
            last_wrd = a
            last_state = state
            pi = self.qgen.refresh_pi(pi, a, last_state[0,0], obj_repr, input_token=True)
            
        dial_len = torch.LongTensor([len(dial) for dial in dialog]).to(device)
        dial_pad = pad_sequence(dialog, batch_first=True, padding_value=pad_token)
        guess = self.guesser(dial_pad, dial_len, cats, bboxs, bboxs_mask)
        return guess, dialog, q_log, a_log, a_conf_log


def oracle_output_to_answer_token(oracle_output, answer2id, answer2token):
    oracle_output = oracle_output.clone()
    yes_indices = oracle_output == answer2id['Yes']
    no_indices = oracle_output == answer2id['No']
    na_indices = oracle_output == answer2id['N/A']
    oracle_output[yes_indices] = answer2token['Yes']
    oracle_output[no_indices] = answer2token['No']
    oracle_output[na_indices] = answer2token['N/A']
    return oracle_output