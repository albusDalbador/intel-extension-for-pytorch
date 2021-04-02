// Autogenerated file by gen-common-ops.py. Do not edit directly!
#pragma once

#include <ATen/Tensor.h>
#include <torch/extension.h>


namespace torch_ipex {

class AtenIpexTypeExt {
 public:
  static at::Tensor ROIAlign_forward(const at::Tensor& input,
                                     const at::Tensor& rois,
                                     const float spatial_scale,
                                     const int pooled_height,
                                     const int pooled_width,
                                     const int sampling_ratio);

  static at::Tensor ROIAlign_backward(const at::Tensor& grad,
                                      const at::Tensor& rois,
                                      const float spatial_scale,
                                      const int pooled_height,
                                      const int pooled_width,
                                      const int batch_size,
                                      const int channels,
                                      const int height,
                                      const int width,
                                      const int sampling_ratio);
    
  static at::Tensor nms(const at::Tensor& dets,
                        const at::Tensor& scores,
                        const float threshold);

  static std::tuple<at::Tensor, at::Tensor, at::Tensor> batch_score_nms(const at::Tensor& dets,
                        const at::Tensor& scores,
                        const float threshold);

  static at::Tensor interaction_forward(const std::vector<at::Tensor> & input);
  static std::vector<at::Tensor> interaction_backward(const at::Tensor & grad_out, 
                                                                     const std::vector<at::Tensor> & input);
};

}  // namespace torch_ipex

