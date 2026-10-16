export const completeDetail = row => {
  const url = 'https://m.media-amazon.com/images/I/fixture.jpg';
  return {...row,title:row.title || 'Fixture drill',specifications:[{label:'Voltage',value:'20 volts'}],main_image_url:url,
    detail_status:'详情页已采集',image_status:'详情页主图',detail_error:null,
    detail_quality:{schema_version:'amazon-detail-quality-v1',title_verified:true,missing_fields:[],
      image:{url,verified:true,decoded:true,http_status:200,mime:'image/jpeg',width:1200,height:1200}}};
};
