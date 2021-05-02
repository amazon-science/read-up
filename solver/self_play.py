# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: CC-BY-NC-4.0
import os
import torch
import torch.nn as nn
from functools import partial
from matplotlib.image import imread
from solver.solver import BaseSolver
from solver.utils import human_format, cal_hit
from torch.utils.data import DataLoader
from src.model.self_play import SelfPlayModel
from src.tools.optimizer import Optimizer
from src.tools.tokenizer import GW_Tokenizer, BERT_Tokenizer
from src.data.image_features_reader import h5FeatureReader as image_features_reader
from src.data.self_play import SelfPlayDataset, collate_fn


NUM_LOG_TEXT_SAMPLES = 5 # must < len(valid_set)


class SelfPlaySolver(BaseSolver):
    def __init__(self, config, args, mode):
        super().__init__(config, args, mode)
        self.step = 0
        self.best_score = -1e10

    def load_dataloader(self, img_feat_readers, tokenizer, splits):
        # Prepare self.train_set, self.valid_set, self.test_set
        dataroot = self.config['data']['dataroot']
        batch_size = self.config['data']['batch_size']
        # splits = ['train', 'valid'] if self.mode == 'train' else ['test', 'valid']
        for split in splits:
            dataset = SelfPlayDataset(
                dataroot, 
                split, 
                img_feat_readers[split], 
                tokenizer, 
                padding_index=tokenizer.pad_id)
            if not hasattr(self, 'answer2id'):    self.answer2id = dataset.answer2id
            if not hasattr(self, 'answer2token'): self.answer2token = dataset.answer2token
            # Set self.XXX_set = torch.utils.data.Dataloader
            setattr(
                self,
                split+'_set',
                DataLoader(
                    dataset,
                    batch_size=batch_size if split == 'train' else 4*batch_size,
                    shuffle=(split=='train'),
                    drop_last=False,
                    collate_fn=partial(collate_fn, wrd_pad_id=tokenizer.pad_id),
                    num_workers=self.args.n_jobs,
                    pin_memory=self.args.pin_memory)
            )


    def load_data(self):
        self.verbose(['Loading data...'])
        config = self.config
        if config['data']['tokenizer'].lower() == 'bert':
            self.verbose(["Use Bert tokenizer"])
            tokenizer = BERT_Tokenizer.from_pretrained(
                'bert-base-uncased', do_lower_case=True)
        else:
            tokenizer = GW_Tokenizer(config['data']['vocab_path'])
        
        feat_path = config['data']['features_path']
        splits = ['train', 'valid'] if self.mode == 'train' else ['test']
        img_feat_readers = {
            split: image_features_reader(feat_path[split]) 
            for split in splits}
        self.load_dataloader(img_feat_readers, tokenizer, splits)
        self.tokenizer = tokenizer
        if self.mode == 'train':
            self.steps_per_epoch = len(self.train_set)
            self.max_step = self.steps_per_epoch * self.max_epoch

    def fetch_data(self, data):
        game, img_feat, tgt_cat, tgt_bbox, cats, bboxs, bboxs_mask, label, qs, q_len = data
        return (
            game,
            img_feat.to(self.device),
            tgt_cat.to(self.device),
            tgt_bbox.to(self.device),
            cats.to(self.device),
            bboxs.to(self.device),
            bboxs_mask.to(self.device),
            label.to(self.device),
            qs.to(self.device),
            q_len.to(self.device),
        )

    def set_model(self):
        self.verbose(['Set model...'])
        self.use_gt_question = 'qgen' not in self.config['model']
        players = ['qgen', 'oracle', 'guesser'] if not self.use_gt_question else \
                  ['oracle', 'guesser']

        print(self.config)
        for plyr in players:
            self.config['model'][plyr]['num_wrds'] = len(self.tokenizer)
            self.config['model'][plyr]['wrd_pad_id'] = self.tokenizer.pad_id

        self.model = SelfPlayModel(
            qgen_kwargs=None if self.use_gt_question else self.config['model']['qgen'] ,
            oracle_kwargs=self.config['model']['oracle'],
            guesser_kwargs=self.config['model']['guesser']
            )
        # Load pretrained players
        for plyr in players:
            log = self.model.load_player(
                plyr, self.config['model'][plyr]['pretrained_path'], map_location="cpu")
            self.verbose([log])
        self.model.to(self.device)
        self.optimizer = Optimizer(
            self.model.parameters(), **self.config['hparas'])
        # self.loss = nn.CrossEntropyLoss(reduction='sum')
        self.loss = nn.CrossEntropyLoss(ignore_index=self.tokenizer.pad_id)

        if self.args.load:
            ckpt = torch.load(self.args.load, map_location=self.device)
            self.model.load_state_dict(ckpt['model'])
            self.optimizer.load_opt_state_dict(ckpt['optimizer'])
            self.step = ckpt['global_step']
            self.verbose('Load ckpt from {}, restarting at step {}'.format(
                self.args.load, self.step))

    def exec(self):
        if self.use_gt_question:
            self.verbose("Use ground truth questions.")
        if self.mode == 'train':
            self.train()
        else:
            self.verbose(["Evaluate on test set..."])
            self.validate(self.test_set)
            # self.verbose(["Evaluate on valid set..."])
            # self.validate(self.valid_set)
        

    def train(self):
        self.verbose(['Total training epoch/steps: {}/{}'.format(
            self.max_epoch, human_format(self.max_step))])
        self.verbose(['Number of steps per epoch: {}'.format(
            human_format(self.steps_per_epoch))])
        while self.step < self.max_step:
            # Validate every epoch
            self.validate(self.valid_set)
            self.timer.set()
            for data in self.train_set:
                game, img_feat, tgt_cat, tgt_bbox, cats, bboxs, bboxs_mask, label, qs, q_len = self.fetch_data(data)
                NOT_IMPLEMENT_YET()
                self.timer.cnt('rd')
                # Forward
                self.optimizer.pre_step(self.step)
                pred = self.model(qgen_in, qgen_in_len, img_feat, mask=None)
                loss = self.loss(pred.view(-1, pred.size(-1)), qgen_tgt.view(-1))
                hit = cal_hit(pred, qgen_tgt)
                acc = hit / float(qgen_tgt.size(0) * qgen_tgt.size(1))
                hit_nopad = cal_hit(pred, qgen_tgt, pad_id=self.tokenizer.pad_id)
                acc_nopad = hit_nopad / float(qgen_tgt.size(0) * qgen_tgt.size(1))
                self.timer.cnt('fw')
                # Backward
                grad_norm = self.backward(loss)
                self.timer.cnt('bw')

                self.step += 1
                # Log
                if (self.step == 1) or (self.step % self._progress_step == 0):
                    self.progress("Tr stat. | Loss - {:.4f} | Acc.(pad/nopad) - {:.3f}/{:.3f} | Grad. norm - {:.2f} | {}".format(
                        loss.item(), acc, acc_nopad, grad_norm, self.timer.show()))            
                    self.write_log('scalars', 'accuracy', {'train': acc})
                    self.write_log('scalars', 'accuracy', {'train-nopad': acc_nopad})
                    self.write_log('scalars', 'loss', {'train': loss})

                # End of step
                self.timer.set()
                if self.step > self.max_step:
                    self.verbose("Reach max training step.")
                    self.logger.close()
                    break


    def validate(self, specified_set):
        self.model.eval()
        total_hit = 0
        total_cnt = 0
        out_file = open('{}.txt'.format(self.exp_name), 'w')
        if not self.use_gt_question:
            out_file.write('game_id|pred_obj|answer_obj|turn_id|question|answer\n')
        
        for val_step, data in enumerate(specified_set):
            game, img_feat, tgt_cat, tgt_bbox, cats, bboxs, bboxs_mask, label, qs, q_len = self.fetch_data(data)
            with torch.no_grad():
                if self.use_gt_question:
                    pred, dialog = self.model.play_with_gt_question(
                        qs, q_len, tgt_cat, tgt_bbox, cats, bboxs, bboxs_mask, 
                        self.tokenizer.pad_id, self.answer2id, self.answer2token)
                    
                else:
                    pred, dialog, q_log, a_log = self.model.play(
                        img_feat, tgt_cat, tgt_bbox, cats, bboxs, bboxs_mask,
                        self.tokenizer.sos_id, self.tokenizer.pad_id, 
                        self.tokenizer.eoq_id, self.tokenizer.eod_id,
                        self.answer2id, self.answer2token,
                        max_q_len=20, greedy=True, max_turns=5
                    )
                if self.use_gt_question:
                    for b in range(pred.size(0)):
                        out_file.write("{}-{}/{} | {}\n".format(
                            game[b].id, pred[b].argmax(dim=-1).item(), label[b].item(), self.tokenizer.decode(dialog[b].tolist())))
                else:
                    for b in range(pred.size(0)):
                        out_prefix = "{}|{}|{}".format(game[b].id, pred[b].argmax(dim=-1).item(), label[b].item())
                        for t in range(len(q_log[b])):
                            out_str = out_prefix + "|{}|{}|".format(t, self.tokenizer.decode(q_log[b][t].tolist()))
                            if t != len(q_log[b])-1:
                                out_str += "{}".format(self.tokenizer.decode(a_log[b][t].tolist()))
                            out_file.write(out_str+'\n')
            
                total_hit += (pred.argmax(dim=-1) == label).sum().item()
                total_cnt += pred.size(0)
                # if (val_step == 0) or ((val_step+1) % self._progress_step == 0):
                self.progress("Dev stat. |  Acc. - {:.3f}".format(total_hit/float(total_cnt)))
                # Log
                if self.mode == 'train':
                    NOT_IMPLEMENT_YET()

                        
        if self.mode == 'train':

            score = -avg_loss
            if score > self.best_score:
                #self.save_checkpoint('step_{}.pth'.format(self.step), score)
                self.save_checkpoint('best.pth', score)
                self.best_score = score
            self.model.train()

        self.verbose(["Val stat. @ step {} | Acc. - {:.3f}"
                      .format(self.step, total_hit / float(total_cnt))])
        
        out_file.close()










