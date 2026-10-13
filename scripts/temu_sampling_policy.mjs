export function samplingDecision(ids, policy, requested) {
 const reference=new Set(policy?.reference_goods_ids||[]);
 const known=ids.filter(id=>reference.has(String(id))).length;
 const overlap=ids.length?known/ids.length:0;
 const reduced=!!policy?.enabled && !policy?.protected && reference.size>0 && ids.length>=policy.minimum_sample && ids.length<requested && overlap>=.9;
 return {requested_products:requested,observed_products:ids.length,reference_products:reference.size,
   new_to_reference:ids.length-known,duplicate_fraction:overlap,protected:!!policy?.protected,
   reduced,reason:reduced?'同排序已采样商品重复率至少90%，保留至少半额且不少于200件后结束扩展词采样':null};
}
