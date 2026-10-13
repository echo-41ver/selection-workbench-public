import test from 'node:test';
import assert from 'node:assert/strict';
import {amazonUsdPrice} from '../scripts/amazon_price.mjs';
test('accept only explicit US dollar amounts, preserve grouping and reject delivery-region currencies',()=>{
  for(const [text,amount] of [['$92.09',92.09],['US$ 1,299.99',1299.99],['USD 44.19',44.19]])assert.equal(amazonUsdPrice(text),amount);
  for(const text of ['CNY 605.05','CN¥605.05','CA$12.00','A$12.00','EUR 13.00','123.00','',null])assert.equal(amazonUsdPrice(text),null);
});
