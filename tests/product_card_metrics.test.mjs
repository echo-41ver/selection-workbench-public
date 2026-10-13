import test from 'node:test';
import assert from 'node:assert/strict';
import {parseProductCardMetrics as parse} from '../scripts/product_card_metrics.mjs';

test('Chinese visible sales and review counts include line wraps and repeated labels', () => {
  const result=parse('无线鼠标\n已售\n6,740件\n已售6,740件\n52条评价\n52');
  assert.equal(result.sales_count,6740);
  assert.equal(result.sales_precision,'displayed');
  assert.equal(result.sales_basis,'页面显示销量，周期未知');
  assert.equal(result.review_count,52);
  assert.equal(result.review_count_text,'52条评价');
  assert.equal(result.rating,null);
});
test('real RAM specification regression never produces rating or review count', () => {
  const result=parse('安卓15平板电脑，64GB存储+12(3+9)GB运行内存\n$56.82\n原价$116.89\n已售5,308件\n978条评价');
  assert.equal(result.rating,null);
  assert.equal(result.review_count,978);
  assert.equal(result.sales_count,5308);
});
test('a price immediately before the sales label is not a second sales value', () => {
  const result=parse('原价 $37.00\n$37.00\n已售\n79件\n已售79件');
  assert.equal(result.sales_count,79);
  assert.equal(result.metric_evidence.sales.status,'AVAILABLE');
});
test('model numbers, price, discount, bare digits and numeric parentheses are not ratings', () => {
  for (const value of ['Mouse 4.8 (200)','$4.8 200','4.8\n(200)','4.8% off','Model M4.8','12(3+9)GB']) {
    const result=parse(value);
    assert.equal(result.rating,null,value);
    assert.equal(result.review_count,null,value);
  }
});
test('explicit English and Chinese ratings and visible accessibility labels work', () => {
  assert.equal(parse('4.8 out of 5 stars').rating,4.8);
  assert.equal(parse('评分：4.8').rating,4.8);
  assert.equal(parse('4.8颗星，满分5颗星').rating,4.8);
  assert.equal(parse('商品标题',['Rated 4.7 out of 5 stars']).rating,4.7);
  assert.equal(parse('商品标题',['Rated 4.7 out of 5 stars']).metric_evidence.rating.source,'visible_label');
});
test('Chinese large lower bounds are not labeled as exact sales', () => {
  const result=parse('已售1.2万+件');
  assert.equal(result.sales_count,12000);
  assert.equal(result.sales_precision,'displayed_lower_bound');
  assert.equal(result.sales_text,'已售1.2万+件');
  assert.equal(parse('銷量：2萬+').sales_count,20000);
  assert.equal(parse('售出１万＋件').sales_count,10000);
});
test('abbreviated counts without plus are explicitly approximate', () => {
  assert.equal(parse('1.2K sold').sales_precision,'displayed_approximate');
  assert.equal(parse('1万条评价').review_count_precision,'displayed_approximate');
  assert.equal(parse('1万条评价').review_count,10000);
});
test('explicit English sales periods and review counts are retained', () => {
  const result=parse('1K+ bought in past month\n246 customer reviews');
  assert.equal(result.sales_count,1000);
  assert.equal(result.sales_basis,'最近一月页面显示');
  assert.equal(result.review_count,246);
  assert.equal(parse('Sold: 205\nReviews: 32').sales_count,205);
});
test('conflicting metrics stay unknown and preserve both observations', () => {
  const result=parse('已售100件',['已售200件','评分4.8','评分4.2']);
  assert.equal(result.sales_count,null);
  assert.equal(result.rating,null);
  assert.equal(result.metric_evidence.sales.status,'CONFLICT');
  assert.equal(result.metric_evidence.sales.observations.length,2);
});
test('invalid rating scales and malformed or fractional product counts are rejected', () => {
  assert.equal(parse('评分5.8').rating,null);
  assert.equal(parse('评分15').rating,null);
  assert.equal(parse('24.8 out of 5').rating,null);
  assert.equal(parse('已售1,2件').sales_count,null);
  assert.equal(parse('1.5 sold').sales_count,null);
});
test('function serializes without any module variables or helpers', () => {
  const browserParse=Function('return ('+parse.toString()+')')();
  assert.deepEqual(browserParse('已售6,012件\n69条评价',['评分4.8']),parse('已售6,012件\n69条评价',['评分4.8']));
});
