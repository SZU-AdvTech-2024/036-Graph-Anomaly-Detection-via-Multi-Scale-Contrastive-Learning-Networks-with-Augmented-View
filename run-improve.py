from model import Model
from utils import *
from sklearn.metrics import roc_auc_score
import random
import os
import dgl
import argparse
from tqdm import tqdm
import torch.nn.functional as F
from sklearn.preprocessing import MinMaxScaler

os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ['OMP_NUM_THREADS'] = '1'

parser = argparse.ArgumentParser(description='GRADATE-IMPROVE')
parser.add_argument('--expid', type=int, default=1)
parser.add_argument('--device', type=str, default='cuda:0')
parser.add_argument('--dataset', type=str, default='eat')
parser.add_argument('--lr', type=float, default=1e-3)
parser.add_argument('--weight_decay', type=float, default=0.0)
parser.add_argument('--runs', type=int, default=1)
parser.add_argument('--embedding_dim', type=int, default=64)
parser.add_argument('--patience', type=int, default=100)
parser.add_argument('--num_epoch', type=int, default=400)
parser.add_argument('--batch_size', type=int, default=300)
parser.add_argument('--subgraph_size', type=int, default=4)
parser.add_argument('--readout', type=str, default='avg')
parser.add_argument('--auc_test_rounds', type=int, default=256)
parser.add_argument('--negsamp_ratio_patch', type=int, default=1)
parser.add_argument('--negsamp_ratio_context', type=int, default=1)
parser.add_argument('--alpha', type=float, default=0.1, help='how much the first view involves')
parser.add_argument('--beta', type=float, default=0.1, help='how much the second view involves')
args = parser.parse_args()

if __name__ == '__main__':

    print('Dataset: {}'.format(args.dataset), flush=True)
    device = torch.device(args.device if torch.cuda.is_available() else 'cpu')
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    all_auc = []

    for run in range(args.runs):
        seed = run + 1
        random.seed(seed)

        batch_size = args.batch_size
        subgraph_size = args.subgraph_size

        adj, features, labels, idx_train, idx_val,\
        idx_test, ano_label, str_ano_label, attr_ano_label = load_mat(args.dataset)

        degree = np.sum(adj, axis=0)
        degree_ave = np.mean(degree)

        features, _ = preprocess_features(features)
        dgl_graph = adj_to_dgl_graph(adj)

        nb_nodes = features.shape[0]  # 节点数
        ft_size = features.shape[1]  # 特征维度
        nb_classes = labels.shape[1]  # 标签个数

        # graph data argumentation 生成第一第二视图
        adj_raw = sp.coo_matrix(adj).todense()
        adj_edge_modification = aug_random_edge(adj, 0.2)
        adj = normalize_adj(adj)
        adj = (adj + sp.eye(adj.shape[0])).todense()  # adj第一视图
        adj_hat = normalize_adj(adj_edge_modification)
        adj_hat = (adj_hat + sp.eye(adj_hat.shape[0])).todense()  # adj第二增强视图

        features = torch.FloatTensor(features[np.newaxis]).to(device)
        adj = torch.FloatTensor(adj[np.newaxis]).to(device)
        adj_hat = torch.FloatTensor(adj_hat[np.newaxis]).to(device)
        labels = torch.FloatTensor(labels[np.newaxis]).to(device)
        idx_train = torch.LongTensor(idx_train).to(device)
        idx_val = torch.LongTensor(idx_val).to(device)
        idx_test = torch.LongTensor(idx_test).to(device)

        print('\n# Run:{} with random seed:{}'.format(run, seed), flush=True)
        dgl.random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)

        os.environ['PYTHONHASHSEED'] = str(seed)

        model = Model(ft_size, args.embedding_dim, 'prelu', args.negsamp_ratio_patch, args.negsamp_ratio_context,
                      args.readout).to(device)
        optimiser = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
        # 节点-子图和节点-节点对比损失
        b_xent_patch = nn.BCEWithLogitsLoss(reduction='none',
                                            pos_weight=torch.tensor([args.negsamp_ratio_patch]).to(device))
        b_xent_context = nn.BCEWithLogitsLoss(reduction='none',
                                            pos_weight=torch.tensor([args.negsamp_ratio_context]).to(device))

        cnt_wait = 0
        best = 1e9
        best_t = 0
        batch_num = nb_nodes // batch_size + 1

        # Train model
        with tqdm(total=args.num_epoch) as pbar:
            pbar.set_description('Training')
            for epoch in range(args.num_epoch):

                model.train()

                all_idx = list(range(nb_nodes))
                random.shuffle(all_idx)
                total_loss = 0.

                subgraphs = generate_rwr_subgraph(dgl_graph, subgraph_size)  # 只采用原始图进行RWR采样

                for batch_idx in range(batch_num):

                    optimiser.zero_grad()

                    is_final_batch = (batch_idx == (batch_num - 1))
                    if not is_final_batch:
                        idx = all_idx[batch_idx * batch_size: (batch_idx + 1) * batch_size]
                    else:
                        idx = all_idx[batch_idx * batch_size:]

                    cur_batch_size = len(idx)

                    lbl_patch = torch.unsqueeze(torch.cat(
                        (torch.ones(cur_batch_size), torch.zeros(cur_batch_size * args.negsamp_ratio_patch))), 1).to(device)

                    lbl_context = torch.unsqueeze(torch.cat(
                        (torch.ones(cur_batch_size), torch.zeros(cur_batch_size * args.negsamp_ratio_context))), 1).to(device)

                    ba = []
                    ba_hat = []
                    bf = []
                    added_adj_zero_row = torch.zeros((cur_batch_size, 1, subgraph_size)).to(device)
                    added_adj_zero_col = torch.zeros((cur_batch_size, subgraph_size + 1, 1)).to(device)
                    added_adj_zero_col[:, -1, :] = 1.
                    added_feat_zero_row = torch.zeros((cur_batch_size, 1, ft_size)).to(device)

                    for i in idx:
                        cur_adj = adj[:, subgraphs[i], :][:, :, subgraphs[i]]  # 第一视图
                        cur_adj_hat = adj_hat[:, subgraphs[i], :][:, :, subgraphs[i]]  # 第二视图
                        cur_feat = features[:, subgraphs[i], :]
                        ba.append(cur_adj)
                        ba_hat.append(cur_adj_hat)
                        bf.append(cur_feat)

                    ba = torch.cat(ba)
                    ba = torch.cat((ba, added_adj_zero_row), dim=1)
                    ba = torch.cat((ba, added_adj_zero_col), dim=2)
                    ba_hat = torch.cat(ba_hat)
                    ba_hat = torch.cat((ba_hat, added_adj_zero_row), dim=1)
                    ba_hat = torch.cat((ba_hat, added_adj_zero_col), dim=2)
                    bf = torch.cat(bf)
                    bf = torch.cat((bf[:, :-1, :], added_feat_zero_row, bf[:, -1:, :]), dim=1)

                    # 节点-子图对比和节点-节点对比
                    logits_1, logits_2, subgraph_embed, node_embed = model(bf, ba)
                    logits_1_hat, logits_2_hat,  subgraph_embed_hat, node_embed_hat = model(bf, ba_hat)

                    # 子图-子图对比
                    subgraph_embed = F.normalize(subgraph_embed, dim=1, p=2)
                    subgraph_embed_hat = F.normalize(subgraph_embed_hat, dim=1, p=2)
                    sim_matrix_one = torch.matmul(subgraph_embed, subgraph_embed_hat.t())  # 嵌入点积
                    sim_matrix_two = torch.matmul(subgraph_embed, subgraph_embed.t())
                    sim_matrix_three = torch.matmul(subgraph_embed_hat, subgraph_embed_hat.t())
                    temperature = 1.0
                    sim_matrix_one_exp = torch.exp(sim_matrix_one / temperature)
                    sim_matrix_two_exp = torch.exp(sim_matrix_two / temperature)
                    sim_matrix_three_exp = torch.exp(sim_matrix_three / temperature)
                    nega_list = np.arange(0, cur_batch_size - 1, 1)
                    nega_list = np.insert(nega_list, 0, cur_batch_size - 1)
                    sim_row_sum = sim_matrix_one_exp[:, nega_list] + sim_matrix_two_exp[:, nega_list]
                    # sim_row_sum = sim_matrix_one_exp[:, nega_list] + sim_matrix_two_exp[:, nega_list] + sim_matrix_three_exp[:, nega_list]
                    sim_row_sum = torch.diagonal(sim_row_sum)
                    sim_diag = torch.diagonal(sim_matrix_one)
                    sim_diag_exp = torch.exp(sim_diag / temperature)
                    NCE_loss = -torch.log(sim_diag_exp / (sim_row_sum))
                    NCE_loss = torch.mean(NCE_loss)

                    # 节点-子图对比
                    loss_all_1 = b_xent_context(logits_1, lbl_context)
                    loss_all_1_hat = b_xent_context(logits_1_hat, lbl_context)
                    loss_1 = torch.mean(loss_all_1)
                    loss_1_hat = torch.mean(loss_all_1_hat)

                    # 节点-节点对比
                    loss_all_2 = b_xent_patch(logits_2, lbl_patch)
                    loss_all_2_hat = b_xent_patch(logits_2_hat, lbl_patch)
                    loss_2 = torch.mean(loss_all_2)
                    loss_2_hat = torch.mean(loss_all_2_hat)

                    loss_1 = args.alpha * loss_1 + (1 - args.alpha) * loss_1_hat  # node-subgraph contrast loss
                    loss_2 = args.alpha * loss_2 + (1 - args.alpha) * loss_2_hat  # node-node contrast loss
                    loss = args.beta * loss_1 + (1 - args.beta) * loss_2 + 0.1 * NCE_loss  # total loss

                    loss.backward()
                    optimiser.step()

                    loss = loss.detach().cpu().numpy()
                    if not is_final_batch:
                        total_loss += loss

                mean_loss = (total_loss * batch_size + loss * cur_batch_size) / nb_nodes

                if mean_loss < best:
                    best = mean_loss
                    best_t = epoch
                    cnt_wait = 0
                    torch.save(model.state_dict(), '{}.pkl'.format(args.dataset))
                else:
                    cnt_wait += 1

                if cnt_wait == args.patience:
                    print('Early stopping!', flush=True)
                    break

                pbar.set_postfix(loss=mean_loss)
                pbar.update(1)

        # Testing
        print('Loading {}th epoch'.format(best_t), flush=True)
        model.load_state_dict(torch.load('{}.pkl'.format(args.dataset)))
        multi_round_ano_score = np.zeros((args.auc_test_rounds, nb_nodes))
        print('Testing AUC!', flush=True)

        nodes_embed = torch.zeros([nb_nodes, args.embedding_dim], dtype=torch.float).cuda()

        with tqdm(total=args.auc_test_rounds) as pbar_test:
            pbar_test.set_description('Testing')
            for round in range(args.auc_test_rounds):
                all_idx = list(range(nb_nodes))
                random.shuffle(all_idx)
                subgraphs = generate_rwr_subgraph(dgl_graph, subgraph_size)
                for batch_idx in range(batch_num):
                    optimiser.zero_grad()
                    is_final_batch = (batch_idx == (batch_num - 1))
                    if not is_final_batch:
                        idx = all_idx[batch_idx * batch_size: (batch_idx + 1) * batch_size]
                    else:
                        idx = all_idx[batch_idx * batch_size:]
                    cur_batch_size = len(idx)
                    ba = []
                    ba_hat = []
                    bf = []
                    added_adj_zero_row = torch.zeros((cur_batch_size, 1, subgraph_size)).to(device)
                    added_adj_zero_col = torch.zeros((cur_batch_size, subgraph_size + 1, 1)).to(device)
                    added_adj_zero_col[:, -1, :] = 1.
                    added_feat_zero_row = torch.zeros((cur_batch_size, 1, ft_size)).to(device)
                    for i in idx:
                        cur_adj = adj[:, subgraphs[i], :][:, :, subgraphs[i]]
                        cur_adj_hat = adj_hat[:, subgraphs[i], :][:, :, subgraphs[i]]
                        cur_feat = features[:, subgraphs[i], :]
                        ba.append(cur_adj)
                        ba_hat.append(cur_adj_hat)
                        bf.append(cur_feat)

                    ba = torch.cat(ba)
                    ba = torch.cat((ba, added_adj_zero_row), dim=1)
                    ba = torch.cat((ba, added_adj_zero_col), dim=2)
                    ba_hat = torch.cat(ba_hat)
                    ba_hat = torch.cat((ba_hat, added_adj_zero_row), dim=1)
                    ba_hat = torch.cat((ba_hat, added_adj_zero_col), dim=2)
                    bf = torch.cat(bf)
                    bf = torch.cat((bf[:, :-1, :], added_feat_zero_row, bf[:, -1:, :]), dim=1)

                    with torch.no_grad():
                        test_logits_1, test_logits_2, _, batch_embed = model(bf, ba)
                        test_logits_1_hat, test_logits_2_hat, _, _ = model(bf, ba_hat)
                        test_logits_1 = torch.sigmoid(torch.squeeze(test_logits_1))
                        test_logits_2 = torch.sigmoid(torch.squeeze(test_logits_2))
                        test_logits_1_hat = torch.sigmoid(torch.squeeze(test_logits_1_hat))
                        test_logits_2_hat = torch.sigmoid(torch.squeeze(test_logits_2_hat))

                        if round == args.auc_test_rounds - 1:
                            nodes_embed[idx] = batch_embed

                        ano_score_1 = - (test_logits_1[:cur_batch_size] - torch.mean(test_logits_1[cur_batch_size:].view(
                            cur_batch_size, args.negsamp_ratio_context), dim=1)).cpu().numpy()
                        ano_score_1_hat = - (
                                    test_logits_1_hat[:cur_batch_size] - torch.mean(test_logits_1_hat[cur_batch_size:].view(
                                cur_batch_size, args.negsamp_ratio_context), dim=1)).cpu().numpy()
                        ano_score_2 = - (test_logits_2[:cur_batch_size] - torch.mean(test_logits_2[cur_batch_size:].view(
                            cur_batch_size, args.negsamp_ratio_patch), dim=1)).cpu().numpy()
                        ano_score_2_hat = - (
                                    test_logits_2_hat[:cur_batch_size] - torch.mean(test_logits_2_hat[cur_batch_size:].view(
                                cur_batch_size, args.negsamp_ratio_patch), dim=1)).cpu().numpy()
                        ano_score = args.beta * (args.alpha * ano_score_1 + (1 - args.alpha) * ano_score_1_hat)  + \
                                    (1 - args.beta) * (args.alpha * ano_score_2 + (1 - args.alpha) * ano_score_2_hat)

                    multi_round_ano_score[round, idx] = ano_score

                pbar_test.update(1)

        # attribute anomaly scores
        attr_ano_score_final = np.mean(multi_round_ano_score, axis=0) + np.std(multi_round_ano_score, axis=0)
        attr_scaler = MinMaxScaler()
        attr_ano_score_final = attr_scaler.fit_transform(attr_ano_score_final.reshape(-1, 1)).reshape(-1)

        # topology anomaly scores
        features_norm = F.normalize(nodes_embed, p=2, dim=1)
        features_similarity = torch.matmul(features_norm, features_norm.transpose(0, 1)).squeeze(0).cpu()

        k_init = int(degree_ave)
        net = nx.from_numpy_matrix(adj_raw)
        net.remove_edges_from(nx.selfloop_edges(net))
        adj_raw = nx.to_numpy_matrix(net)
        multi_round_stru_ano_score = []
        while 1:
            list_temp = list(nx.k_core(net, k_init))
            if list_temp == []:
                break
            else:
                core_adj = adj_raw[list_temp, :][:, list_temp]
                core_graph = nx.from_numpy_matrix(core_adj)
                list_temp = np.array(list_temp)
                for i in nx.connected_components(core_graph):
                    core_temp = list(i)
                    core_temp = list_temp[core_temp]
                    core_temp_size = len(core_temp)
                    similar_temp = 0
                    similar_num = 0
                    scores_temp = np.zeros(nb_nodes)
                    for idx in core_temp:
                        for idy in core_temp:
                            if idx != idy:
                                similar_temp += features_similarity[idx][idy]
                                similar_num += 1
                    scores_temp[core_temp] = core_temp_size * 1 / (similar_temp / similar_num)
                    multi_round_stru_ano_score.append(scores_temp)
                k_init += 1

        multi_round_stru_ano_score = np.array(multi_round_stru_ano_score)
        multi_round_stru_ano_score = np.mean(multi_round_stru_ano_score, axis=0)
        stru_scaler = MinMaxScaler()
        stru_ano_score_final = stru_scaler.fit_transform(multi_round_stru_ano_score.reshape(-1, 1)).reshape(-1)

        alpha_list = list(np.arange(0, 1, 0.2))
        rate_auc = []
        for alpha in alpha_list:
            final_scores_rate = alpha * attr_ano_score_final + (1 - alpha) * stru_ano_score_final
            auc_temp = roc_auc_score(ano_label, final_scores_rate)
            rate_auc.append(auc_temp)
        max_alpha = alpha_list[rate_auc.index(max(rate_auc))]
        final_scores_rate = max_alpha * attr_ano_score_final + (1 - max_alpha) * stru_ano_score_final
        best_auc = roc_auc_score(ano_label, final_scores_rate)
        print('Alpha: ', max_alpha)
        print('AUC:{:.4f}'.format(best_auc))
        print('\n')
        all_auc.append(best_auc)

    print('\n==============================')
    print(all_auc)
    print('FINAL TESTING AUC:{:.4f}'.format(np.mean(all_auc)))
    print('==============================')
