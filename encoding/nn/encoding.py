##+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++
## Created by: Hang Zhang
## ECE Department, Rutgers University
## Email: zhang.hang@rutgers.edu
## Copyright (c) 2017
##
## This source code is licensed under the MIT-style license found in the
## LICENSE file in the root directory of this source tree 
##+++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++

import threading
import torch
from torch.nn import Module, Parameter
import torch.nn.functional as F
from torch.autograd import Function, Variable

from .._ext import encoding_lib
from ..functions import scaledL2, aggregate
from ..parallel import my_data_parallel
from ..functions import dilatedavgpool2d

__all__ = ['Encoding', 'EncodingShake', 'Inspiration', 'DilatedAvgPool2d', 'UpsampleConv2d'] 

class Encoding(Module):
    r"""
    Encoding Layer: a learnable residual encoder over 3d or 4d input that 
    is seen as a mini-batch.

    .. image:: _static/img/cvpr17.svg
        :width: 50%
        :align: center

    .. math::

        e_{ik} = \frac{exp(-s_k\|x_{i}-c_k\|^2)}{\sum_{j=1}^K exp(-s_j\|x_{i}-c_j\|^2)} (x_i - c_k)

    Please see the `example of training Deep TEN <./experiments/texture.html>`_.

    Reference:
        Hang Zhang, Jia Xue, and Kristin Dana. "Deep TEN: Texture Encoding Network." *The IEEE Conference on Computer Vision and Pattern Recognition (CVPR) 2017*

    Args:
        D: dimention of the features or feature channels
        K: number of codeswords

    Shape:
        - Input: :math:`X\in\mathcal{R}^{B\times N\times D}` or :math:`\mathcal{R}^{B\times D\times H\times W}` (where :math:`B` is batch, :math:`N` is total number of features or :math:`H\times W`.)
        - Output: :math:`E\in\mathcal{R}^{B\times K\times D}`
        
    Attributes:
        codewords (Tensor): the learnable codewords of shape (:math:`K\times D`)
        scale (Tensor): the learnable scale factor of visual centers

    Examples:
        >>> import encoding
        >>> import torch
        >>> import torch.nn.functional as F
        >>> from torch.autograd import Variable
        >>> B,C,H,W,K = 2,3,4,5,6
        >>> X = Variable(torch.cuda.DoubleTensor(B,C,H,W).uniform_(-0.5,0.5), requires_grad=True)
        >>> layer = encoding.Encoding(C,K).double().cuda()
        >>> E = layer(X)
    """
    def __init__(self, D, K):
        super(Encoding, self).__init__()
        # init codewords and smoothing factor
        self.D, self.K = D, K
        self.codewords = Parameter(torch.Tensor(K, D), 
            requires_grad=True)
        self.scale = Parameter(torch.Tensor(K), requires_grad=True) 
        self.reset_params()
        
    def reset_params(self):
        std1 = 1./((self.K*self.D)**(1/2))
        self.codewords.data.uniform_(-std1, std1)
        self.scale.data.uniform_(-1, 0)

    def forward(self, X):
        if isinstance(X, tuple) or isinstance(X, list):
            # for self-parallel mode, please see encoding.nn
            return my_data_parallel(self, X)
        elif not isinstance(X, Variable):
            raise RuntimeError('unknown input type')
        # input X is a 4D tensor
        assert(X.size(1)==self.D)
        if X.dim() == 3:
            # BxDxN
            B, N, K, D = X.size(0), X.size(2), self.K, self.D
            X = X.transpose(1,2).contiguous()
        elif X.dim() == 4:
            # BxDxHxW
            B, N, K, D = X.size(0), X.size(2)*X.size(3), self.K, self.D
            X = X.view(B,D,-1).transpose(1,2).contiguous()
        else:
            raise RuntimeError('Encoding Layer unknown input dims!')
        # assignment weights NxKxD
        A = F.softmax(scaledL2(X, self.codewords, self.scale), dim=1)
        # aggregate
        E = aggregate(A, X, self.codewords)
        return E

    def __repr__(self):
        return self.__class__.__name__ + '(' \
            + 'N x ' + str(self.D) + '=>' + str(self.K) + 'x' \
            + str(self.D) + ')'

class EncodingShake(Module):
    def __init__(self, D, K):
        super(EncodingShake, self).__init__()
        # init codewords and smoothing factor
        self.D, self.K = D, K
        self.codewords = Parameter(torch.Tensor(K, D), 
            requires_grad=True)
        self.scale = Parameter(torch.Tensor(K), requires_grad=True) 
        self.reset_params()
        
    def reset_params(self):
        std1 = 1./((self.K*self.D)**(1/2))
        self.codewords.data.uniform_(-std1, std1)
        self.scale.data.uniform_(-1, 0)

    def shake(self):
        if self.training:
            self.scale.data.uniform_(-1, 0)
        else:
            self.scale.data.zero_().add_(-0.5)

    def forward(self, X):
        if isinstance(X, tuple) or isinstance(X, list):
            # for self-parallel mode, please see encoding.nn
            return my_data_parallel(self, X)
        elif not isinstance(X, Variable):
            raise RuntimeError('unknown input type')
        # input X is a 4D tensor
        assert(X.size(1)==self.D)
        if X.dim() == 3:
            # BxDxN
            B, N, K, D = X.size(0), X.size(2), self.K, self.D
            X = X.transpose(1,2).contiguous()
        elif X.dim() == 4:
            # BxDxHxW
            B, N, K, D = X.size(0), X.size(2)*X.size(3), self.K, self.D
            X = X.view(B,D,-1).transpose(1,2).contiguous()
        else:
            raise RuntimeError('Encoding Layer unknown input dims!')
        # shake
        self.shake()
        # assignment weights
        A = F.softmax(scaledL2(X, self.codewords, self.scale), dim=1)
        # aggregate
        E = aggregate(A, X, self.codewords)
        # shake
        self.shake()
        return E

    def __repr__(self):
        return self.__class__.__name__ + '(' \
            + 'N x ' + str(self.D) + '=>' + str(self.K) + 'x' \
            + str(self.D) + ')'


class Inspiration(Module):
    r""" 
    Inspiration Layer (CoMatch Layer) enables the multi-style transfer in feed-forward network, which learns to match the target feature statistics during the training. 
    This module is differentialble and can be inserted in standard feed-forward network to be learned directly from the loss function without additional supervision. 

    .. math::
        Y = \phi^{-1}[\phi(\mathcal{F}^T)W\mathcal{G}]

    Please see the `example of MSG-Net <./experiments/style.html>`_  
    training multi-style generative network for real-time transfer.

    Reference:
        Hang Zhang and Kristin Dana. "Multi-style Generative Network for Real-time Transfer."  *arXiv preprint arXiv:1703.06953 (2017)*
    """
    def __init__(self, C, B=1):
        super(Inspiration, self).__init__()
        # B is equal to 1 or input mini_batch
        self.weight = Parameter(torch.Tensor(1,C,C), requires_grad=True)
        # non-parameter buffer
        self.G = Variable(torch.Tensor(B,C,C), requires_grad=True)
        self.C = C
        self.reset_parameters()

    def reset_parameters(self):
        self.weight.data.uniform_(0.0, 0.02)

    def setTarget(self, target):
        self.G = target

    def forward(self, X):
        # input X is a 3D feature map
        self.P = torch.bmm(self.weight.expand_as(self.G),self.G)
        return torch.bmm(self.P.transpose(1,2).expand(X.size(0), self.C, self.C), X.view(X.size(0),X.size(1),-1)).view_as(X)

    def __repr__(self):
        return self.__class__.__name__ + '(' \
            + 'N x ' + str(self.C) + ')'


class DilatedAvgPool2d(Module):
    r"""We provide Dilated Average Pooling for the dilation of Densenet as
    in :class:`encoding.dilated.DenseNet`.

    Reference::
        We provide this code for a comming paper.

    Applies a 2D average pooling over an input signal composed of several input planes.

    In the simplest case, the output value of the layer with input size :math:`(N, C, H, W)`,
    output :math:`(N, C, H_{out}, W_{out})` and :attr:`kernel_size` :math:`(kH, kW)`
    can be precisely described as:

    .. math::

        \begin{array}{ll}
        out(b, c, h, w)  = 1 / (kH * kW) * 
        \sum_{{m}=0}^{kH-1} \sum_{{n}=0}^{kW-1}
        input(b, c, dH * h + m, dW * w + n)
        \end{array}

    | If :attr:`padding` is non-zero, then the input is implicitly zero-padded on both sides
      for :attr:`padding` number of points

    The parameters :attr:`kernel_size`, :attr:`stride`, :attr:`padding`, :attr:`dilation` can either be:

        - a single ``int`` -- in which case the same value is used for the height and width dimension
        - a ``tuple`` of two ints -- in which case, the first `int` is used for the height dimension,
          and the second `int` for the width dimension

    Args:
        kernel_size: the size of the window
        stride: the stride of the window. Default value is :attr:`kernel_size`
        padding: implicit zero padding to be added on both sides
        dilation: the dilation parameter similar to Conv2d

    Shape:
        - Input: :math:`(N, C, H_{in}, W_{in})`
        - Output: :math:`(N, C, H_{out}, W_{out})` where
          :math:`H_{out} = floor((H_{in}  + 2 * padding[0] - kernel\_size[0]) / stride[0] + 1)`
          :math:`W_{out} = floor((W_{in}  + 2 * padding[1] - kernel\_size[1]) / stride[1] + 1)`

    Examples::

        >>> # pool of square window of size=3, stride=2, dilation=2
        >>> m = nn.DilatedAvgPool2d(3, stride=2, dilation=2)
        >>> input = autograd.Variable(torch.randn(20, 16, 50, 32))
        >>> output = m(input)

    """
    def __init__(self, kernel_size, stride=None, padding=0, dilation=1):
        super(DilatedAvgPool2d, self).__init__()
        self.kernel_size = kernel_size
        self.stride = stride or kernel_size
        self.padding = padding
        self.dilation = dilation

    def forward(self, input):
        if isinstance(input, Variable):
            return dilatedavgpool2d(input, self.kernel_size, self.stride,
                                self.padding, self.dilation)
        elif isinstance(input, tuple) or isinstance(input, list):
            return my_data_parallel(self, input)
        else:
            raise RuntimeError('unknown input type')

    def __repr__(self):
        return self.__class__.__name__ + ' (' \
            + 'size=' + str(self.kernel_size) \
            + ', stride=' + str(self.stride) \
            + ', padding=' + str(self.padding) \
            + ', dilation=' + str(self.dilation) + ')'


class UpsampleConv2d(Module):
    r"""
    To avoid the checkerboard artifacts of standard Fractionally-strided Convolution, we adapt an integer stride convolution but producing a :math:`2\times 2` outputs for each convolutional window. 

    .. image:: _static/img/upconv.png
        :width: 50%
        :align: center

    Reference:
        Hang Zhang and Kristin Dana. "Multi-style Generative Network for Real-time Transfer."  *arXiv preprint arXiv:1703.06953 (2017)*

    Args:
        in_channels (int): Number of channels in the input image
        out_channels (int): Number of channels produced by the convolution
        kernel_size (int or tuple): Size of the convolving kernel
        stride (int or tuple, optional): Stride of the convolution. Default: 1
        padding (int or tuple, optional): Zero-padding added to both sides of the input. Default: 0
        output_padding (int or tuple, optional): Zero-padding added to one side of the output. Default: 0
        groups (int, optional): Number of blocked connections from input channels to output channels. Default: 1
        bias (bool, optional): If True, adds a learnable bias to the output. Default: True
        dilation (int or tuple, optional): Spacing between kernel elements. Default: 1
        scale_factor (int): scaling factor for upsampling convolution. Default: 1

    Shape:
        - Input: :math:`(N, C_{in}, H_{in}, W_{in})`
        - Output: :math:`(N, C_{out}, H_{out}, W_{out})` where
          :math:`H_{out} = scale * (H_{in} - 1) * stride[0] - 2 * padding[0] + kernel\_size[0] + output\_padding[0]`
          :math:`W_{out} = scale * (W_{in} - 1) * stride[1] - 2 * padding[1] + kernel\_size[1] + output\_padding[1]`

    Attributes:
        weight (Tensor): the learnable weights of the module of shape
                         (in_channels, scale * scale * out_channels, kernel_size[0], kernel_size[1])
        bias (Tensor):   the learnable bias of the module of shape (scale * scale * out_channels)

    Examples::
        >>> # With square kernels and equal stride
        >>> m = nn.UpsampleCov2d(16, 33, 3, stride=2)
        >>> # non-square kernels and unequal stride and with padding
        >>> m = nn.UpsampleCov2d(16, 33, (3, 5), stride=(2, 1), padding=(4, 2))
        >>> input = autograd.Variable(torch.randn(20, 16, 50, 100))
        >>> output = m(input)
        >>> # exact output size can be also specified as an argument
        >>> input = autograd.Variable(torch.randn(1, 16, 12, 12))
        >>> downsample = nn.Conv2d(16, 16, 3, stride=2, padding=1)
        >>> upsample = nn.UpsampleCov2d(16, 16, 3, stride=2, padding=1)
        >>> h = downsample(input)
        >>> h.size()
        torch.Size([1, 16, 6, 6])
        >>> output = upsample(h, output_size=input.size())
        >>> output.size()
        torch.Size([1, 16, 12, 12])

    """
    def __init__(self, in_channels, out_channels, kernel_size, stride=1,
                 padding=0, dilation=1, groups=1, scale_factor =1, 
                 bias=True):
        super(UpsampleConv2d, self).__init__()
        kernel_size = _pair(kernel_size)
        stride = _pair(stride)
        padding = _pair(padding)
        dilation = _pair(dilation)
        if in_channels % groups != 0:
            raise ValueError('in_channels must be divisible by groups')
        if out_channels % groups != 0:
            raise ValueError('out_channels must be divisible by groups')
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding
        self.dilation = dilation
        self.groups = groups
        self.scale_factor = scale_factor
        self.weight = Parameter(torch.Tensor(
            out_channels * scale_factor * scale_factor, 
            in_channels // groups, *kernel_size))
        if bias:
            self.bias = Parameter(torch.Tensor(out_channels * 
                scale_factor * scale_factor))
        else:
            self.register_parameter('bias', None)
        self.reset_parameters()

    def reset_parameters(self):
        n = self.in_channels
        for k in self.kernel_size:
            n *= k
        stdv = 1. / math.sqrt(n)
        self.weight.data.uniform_(-stdv, stdv)
        if self.bias is not None:
            self.bias.data.uniform_(-stdv, stdv)

    def forward(self, input):
        if isinstance(input, Variable):
            out = F.conv2d(input, self.weight, self.bias, self.stride,
                            self.padding, self.dilation, self.groups)
            return F.pixel_shuffle(out, self.scale_factor)
        elif isinstance(input, tuple) or isinstance(input, list):
            return my_data_parallel(self, input)
        else:
            raise RuntimeError('unknown input type')

